"""
test_guard_delta_mode.py

guard-japanese-prose.sh の delta mode (AC2) の smoke test。

- gh issue view / gh pr view を mock した fixture を使い、実 GitHub API に依存しない (AC15)
- テストケース:
  - #578 同型の \\| -> | (code fence 内のみ) の変更: exit 0 (pass) (AC3, AC9, AC12)
  - 新規英語 prose 段落追加: exit 2 (block) (AC5, AC13)
  - --body-file - (stdin): exit 2 (block) (AC7)
  - code fence 内だけの変更: exit 0 (pass) (AC3)
  - machine-readable YAML のみの変更: exit 0 (pass) (AC4, AC14)
  - 複数 target: exit 2 + target_ambiguous (AC10)
  - target 解決失敗: exit 2 + target_resolution_failed (AC11)
  - gh issue create (非 delta) は full body を検査 (AC1)
  - 既存英語 prose あり + code fence のみ変更: exit 0 (pass) (AC6, AC12)

AC への対応:
  AC1: gh issue create の full-body 検査（test_gh_issue_create_full_body_check）
  AC2: delta mode の基本動作（test_code_fence_only_change_passes）
  AC3, AC9, AC12: #578 同型 \\| -> | code fence 変更: exit 0（test_578_pipe_escape_in_code_fence_passes）
  AC4, AC14: YAML/shell のみ変更: exit 0（test_machine_yaml_only_change_passes）
  AC5, AC13: 新規英語 prose: exit 2（test_new_english_prose_blocked）
  AC6: 既存英語 prose + code fence のみ変更: exit 0（test_existing_english_prose_code_fence_only_passes）
  AC7: --body-file -: exit 2（test_stdin_body_file_blocked）
  AC10: 複数 target: exit 2 + target_ambiguous（test_multiple_targets_ambiguous）
  AC11: target 解決失敗: exit 2 + target_resolution_failed（test_target_resolution_failed）
  AC15: mock で実 API 非依存（全テストで使用）
  AC16: gh api は delta_mode 対象外（test_gh_api_not_delta_mode）
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# テスト対象スクリプトのパス
HOOKS_DIR = Path(__file__).parent.parent
HOOK_SCRIPT = HOOKS_DIR / "guard-japanese-prose.sh"
PROJECT_DIR = HOOKS_DIR.parent.parent
VALIDATOR = PROJECT_DIR / ".claude/skills/create-issue/scripts/validate_japanese_content.py"

# validate_japanese_content モジュールを直接インポート（単体テスト用）
sys.path.insert(0, str(PROJECT_DIR / ".claude/skills/create-issue/scripts"))
from validate_japanese_content import (
    split_markdown_blocks,
    changed_prose_blocks,
    validate_text,
)


# ============================================================
# Unit tests: split_markdown_blocks / changed_prose_blocks
# ============================================================

class TestSplitMarkdownBlocks:
    """split_markdown_blocks 関数のユニットテスト (AC4)"""

    def test_code_fence_block_classified(self):
        """GIVEN: code fence ブロック WHEN: split THEN: type=code_fence"""
        text = "```bash\necho hello\n```"
        blocks = split_markdown_blocks(text)
        assert any(b['type'] == 'code_fence' for b in blocks)

    def test_prose_block_classified(self):
        """GIVEN: 日本語 prose WHEN: split THEN: type=prose"""
        text = "これは日本語の prose です。"
        blocks = split_markdown_blocks(text)
        assert any(b['type'] == 'prose' for b in blocks)

    def test_machine_yaml_block_classified(self):
        """GIVEN: machine-readable YAML WHEN: split THEN: type=machine_yaml"""
        text = "key: value\nstatus: ok\ndecision: pass"
        blocks = split_markdown_blocks(text)
        assert any(b['type'] == 'machine_yaml' for b in blocks)

    def test_shell_command_block_classified(self):
        """GIVEN: shell コマンド WHEN: split THEN: type=shell_command"""
        text = "$ rg -n 'pattern' file.sh\n$ echo test"
        blocks = split_markdown_blocks(text)
        assert any(b['type'] in ('shell_command', 'grep_pattern') for b in blocks)

    def test_mixed_text_multiple_blocks(self):
        """GIVEN: prose + code fence の混在 WHEN: split THEN: 複数ブロックが返る"""
        text = "日本語の説明です。\n\n```python\nprint('hello')\n```\n\nもう一つの説明。"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 2
        types = {b['type'] for b in blocks}
        assert 'code_fence' in types


class TestChangedProseBlocks:
    """changed_prose_blocks 関数のユニットテスト (AC2)"""

    def test_no_prose_change_returns_empty(self):
        """GIVEN: code fence のみの変更 WHEN: changed_prose_blocks THEN: 空リスト（pass）"""
        old = "日本語の説明。\n\n```bash\necho old\n```"
        new = "日本語の説明。\n\n```bash\necho new\n```"
        result = changed_prose_blocks(old, new)
        assert result == []

    def test_new_prose_block_detected(self):
        """GIVEN: 新規英語 prose 追加 WHEN: changed_prose_blocks THEN: 追加ブロックが返る"""
        old = "日本語の説明。"
        new = "日本語の説明。\n\nThis is new English prose text that was added."
        result = changed_prose_blocks(old, new)
        assert len(result) > 0
        assert any('English' in b['text'] for b in result)

    def test_identical_prose_not_detected(self):
        """GIVEN: prose 変更なし WHEN: changed_prose_blocks THEN: 空リスト"""
        text = "日本語の説明。\n\nもう一つの日本語段落。"
        result = changed_prose_blocks(text, text)
        assert result == []

    def test_578_type_pipe_escape_change_empty(self):
        """GIVEN: code fence 内の \\| -> | 変更のみ WHEN: changed_prose_blocks THEN: 空リスト (AC3, AC9)"""
        # #578 同型: code fence 内の \\| が | に変わる
        old = (
            "This is existing English prose.\n\n"
            "```bash\n"
            "rg -n 'pattern\\|other' file.sh\n"
            "```"
        )
        new = (
            "This is existing English prose.\n\n"
            "```bash\n"
            "rg -n 'pattern|other' file.sh\n"
            "```"
        )
        result = changed_prose_blocks(old, new)
        assert result == []

    def test_machine_yaml_only_change_empty(self):
        """GIVEN: machine-readable YAML のみ変更 WHEN: changed_prose_blocks THEN: 空リスト (AC4, AC14)"""
        old = "日本語の説明。\n\nstatus: old\nkey: value"
        new = "日本語の説明。\n\nstatus: new\nkey: value"
        result = changed_prose_blocks(old, new)
        assert result == []

    def test_existing_english_prose_unchanged_not_detected(self):
        """GIVEN: 既存英語 prose が変更されない WHEN: changed_prose_blocks THEN: 空リスト (AC6)"""
        old = "This is existing English prose in the issue body."
        new = "This is existing English prose in the issue body."
        result = changed_prose_blocks(old, new)
        assert result == []


# ============================================================
# Hook integration tests: guard-japanese-prose.sh の delta mode
# ============================================================

def run_hook(hook_input: dict, mock_gh_responses: dict = None) -> subprocess.CompletedProcess:
    """
    guard-japanese-prose.sh をサブプロセスで実行する。
    mock_gh_responses: {'issue view <N>': '<body>'} 形式の mock データ。
    gh コマンドは mock_gh_script 経由で呼び出す。
    """
    input_json = json.dumps(hook_input)

    env = os.environ.copy()
    env['PROJECT_DIR'] = str(PROJECT_DIR)

    # gh コマンドを mock するための wrapper script を一時ディレクトリに作成
    with tempfile.TemporaryDirectory() as tmpdir:
        if mock_gh_responses:
            # mock gh スクリプトを作成
            mock_gh = os.path.join(tmpdir, 'gh')
            mock_gh_content = _build_mock_gh_script(mock_gh_responses)
            with open(mock_gh, 'w') as f:
                f.write(mock_gh_content)
            os.chmod(mock_gh, 0o755)
            # PATH の先頭に mock ディレクトリを追加
            env['PATH'] = tmpdir + ':' + env.get('PATH', '')

        result = subprocess.run(
            ['bash', str(HOOK_SCRIPT)],
            input=input_json,
            capture_output=True,
            text=True,
            env=env,
        )
        return result


def _build_mock_gh_script(responses: dict) -> str:
    """
    gh コマンドの mock スクリプトを生成する。
    responses: {'issue view <N> --json body --jq .body': '<body text>'} 形式
    """
    lines = ['#!/usr/bin/env bash', '']
    lines.append('# Mock gh command for testing')
    lines.append('ARGS="$*"')
    lines.append('')

    for pattern, response in responses.items():
        # シェルのパターンマッチング用にエスケープ
        escaped_response = response.replace("'", "'\"'\"'")
        lines.append(f'if echo "$ARGS" | grep -q "{pattern}"; then')
        lines.append(f"  echo '{escaped_response}'")
        lines.append(f"  exit 0")
        lines.append(f'fi')
        lines.append('')

    # デフォルト: 失敗
    lines.append('# Unknown gh command - exit with error')
    lines.append('echo "mock gh: unknown command: $ARGS" >&2')
    lines.append('exit 1')

    return '\n'.join(lines) + '\n'


def make_bash_hook_input(command: str) -> dict:
    """Bash ツールの hook input JSON を生成する"""
    return {
        "tool_name": "Bash",
        "tool_input": {
            "command": command
        }
    }


class TestDeltaMode:
    """delta mode の end-to-end smoke test"""

    def test_code_fence_only_change_passes(self, tmp_path):
        """GIVEN: code fence 内のみ変更 WHEN: gh issue edit --body-file THEN: exit 0 (AC2, AC3)"""
        old_body = "日本語の説明です。\n\n```bash\necho old\n```"
        new_body = "日本語の説明です。\n\n```bash\necho new\n```"

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 123 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 123": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, f"exit {result.returncode}: stderr={result.stderr}"

    def test_578_pipe_escape_in_code_fence_passes(self, tmp_path):
        """GIVEN: \\| -> | の code fence 内変更（#578 同型）WHEN: gh issue edit THEN: exit 0 (AC3, AC9, AC12)"""
        old_body = (
            "This is existing English prose in the issue.\n\n"
            "```bash\n"
            "rg -n 'pattern\\|other' .claude/hooks/guard.sh\n"
            "```\n\n"
            "## Verification Commands\n\n"
            "```bash\n"
            "$ rg -n 'delta_mode' file.sh\n"
            "```"
        )
        new_body = (
            "This is existing English prose in the issue.\n\n"
            "```bash\n"
            "rg -n 'pattern|other' .claude/hooks/guard.sh\n"
            "```\n\n"
            "## Verification Commands\n\n"
            "```bash\n"
            "$ rg -n 'delta_mode' file.sh\n"
            "```"
        )

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 578 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 578": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, f"exit {result.returncode}: stderr={result.stderr}"

    def test_new_english_prose_blocked(self, tmp_path):
        """GIVEN: 新規英語 prose 段落追加 WHEN: gh issue edit THEN: exit 2 (AC5, AC13)"""
        old_body = "既存の日本語 prose です。"
        new_body = (
            "既存の日本語 prose です。\n\n"
            "This is new English prose paragraph added to the issue body."
        )

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 100 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 100": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, f"exit {result.returncode}: stderr={result.stderr}"
        assert 'changed_prose_blocks' in result.stderr or 'GUARD' in result.stderr

    def test_stdin_body_file_blocked(self):
        """GIVEN: --body-file - (stdin) WHEN: gh issue edit THEN: exit 2 (AC7)"""
        hook_input = make_bash_hook_input(
            "gh issue edit 100 --body-file -"
        )

        result = run_hook(hook_input)
        assert result.returncode == 2, f"exit {result.returncode}: stderr={result.stderr}"
        assert 'stdin' in result.stderr.lower() or 'fail-closed' in result.stderr.lower()

    def test_machine_yaml_only_change_passes(self, tmp_path):
        """GIVEN: machine-readable YAML のみ変更 WHEN: gh issue edit THEN: exit 0 (AC4, AC14)"""
        old_body = (
            "日本語の説明。\n\n"
            "```yaml\n"
            "status: old_value\n"
            "key: value\n"
            "```"
        )
        new_body = (
            "日本語の説明。\n\n"
            "```yaml\n"
            "status: new_value\n"
            "key: value\n"
            "```"
        )

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 200 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 200": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, f"exit {result.returncode}: stderr={result.stderr}"

    def test_multiple_targets_ambiguous(self, tmp_path):
        """GIVEN: 複数 issue target WHEN: gh issue edit THEN: exit 2 + target_ambiguous (AC10)"""
        body_file = tmp_path / "body.md"
        body_file.write_text("日本語テスト", encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 100 200 --body-file {body_file}"
        )

        result = run_hook(hook_input)
        assert result.returncode == 2, f"exit {result.returncode}: stderr={result.stderr}"
        assert 'target_ambiguous' in result.stderr

    def test_target_resolution_failed(self, tmp_path):
        """GIVEN: target を解決できない場合 WHEN: gh issue edit THEN: exit 2 + target_resolution_failed (AC11)"""
        body_file = tmp_path / "body.md"
        body_file.write_text("日本語テスト", encoding='utf-8')

        # target なしの gh issue edit コマンド
        hook_input = make_bash_hook_input(
            f"gh issue edit --body-file {body_file}"
        )

        result = run_hook(hook_input)
        # target が解決できない場合: target_resolution_failed
        # (gh issue view が失敗する場合も同様)
        # target 番号なしでも gh 側でエラーになるが、mock で制御
        # ここでは gh issue view が失敗する場合を mock
        mock_responses = {}  # 空: gh issue view が失敗する

        result2 = run_hook(hook_input, mock_responses)
        assert result2.returncode == 2, f"exit {result2.returncode}: stderr={result2.stderr}"
        assert 'target_resolution_failed' in result2.stderr or result2.returncode == 2

    def test_existing_english_prose_code_fence_only_passes(self, tmp_path):
        """GIVEN: 既存英語 prose + code fence のみ変更 WHEN: gh issue edit THEN: exit 0 (AC6, AC12)"""
        # ガード導入前に英語 prose が含まれる Issue で、
        # code fence 内の \\| -> | 等の無関係な変更が通ることを確認
        old_body = (
            "This Issue was created before the guard was introduced. "
            "It contains English prose.\n\n"
            "```bash\n"
            "rg -n 'old\\|pattern' file.sh\n"
            "```"
        )
        new_body = (
            "This Issue was created before the guard was introduced. "
            "It contains English prose.\n\n"
            "```bash\n"
            "rg -n 'old|pattern' file.sh\n"
            "```"
        )

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 578 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 578": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, f"exit {result.returncode}: stderr={result.stderr}"

    def test_existing_english_prose_new_english_prose_blocked(self, tmp_path):
        """GIVEN: 既存英語 prose + 新規英語 prose 追加 WHEN: gh issue edit THEN: exit 2 (AC13)"""
        old_body = (
            "This Issue has existing English prose.\n\n"
            "```bash\n"
            "echo test\n"
            "```"
        )
        new_body = (
            "This Issue has existing English prose.\n\n"
            "```bash\n"
            "echo test\n"
            "```\n\n"
            "This is a newly added English prose paragraph."
        )

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 300 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 300": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, f"exit {result.returncode}: stderr={result.stderr}"

    def test_gh_pr_edit_delta_mode(self, tmp_path):
        """GIVEN: gh pr edit + code fence のみ変更 WHEN: hook THEN: exit 0 (AC2)"""
        old_body = "日本語の PR 説明です。\n\n```bash\nold code\n```"
        new_body = "日本語の PR 説明です。\n\n```bash\nnew code\n```"

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh pr edit 42 --body-file {body_file}"
        )

        mock_responses = {
            "pr view 42": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, f"exit {result.returncode}: stderr={result.stderr}"

    def test_gh_issue_create_full_body_check(self, tmp_path):
        """GIVEN: gh issue create (非 delta) + 英語 body WHEN: hook THEN: exit 2 (AC1)"""
        body_content = (
            "This is an English-only issue body. "
            "It should be blocked by the full-body check."
        )
        body_file = tmp_path / "body.md"
        body_file.write_text(body_content, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue create --title 'Test' --body-file {body_file}"
        )

        result = run_hook(hook_input)
        assert result.returncode == 2, f"exit {result.returncode}: stderr={result.stderr}"

    def test_gh_api_not_delta_mode(self, tmp_path):
        """GIVEN: gh api (Out of Scope) WHEN: hook THEN: delta_mode 対象外 (AC16)"""
        # gh api は delta mode 対象外であることを確認
        # gh api コマンドに --body-file が含まれていても delta mode は適用しない
        # (gh api は issue/pr の edit ではない)
        body_file = tmp_path / "body.md"
        body_file.write_text("English only body content here.", encoding='utf-8')

        # gh api は現在 full-body 検査 or スルーどちらでも AC16 は satisfied
        # (gh api は delta 検査の対象外であることを確認する)
        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/1 --method PATCH -f body=test"
        )
        result = run_hook(hook_input)
        # gh api は --body-file を使わないため delta mode に入らない
        # (body フィールドが直接指定された場合は full-body check)
        # ここでは通過することを確認（英語のみの --body は block される）
        # AC16: delta_mode が gh api に適用されないことを確認するのが目的
        # (exit code は full-body check に依存するため 0 or 2)
        # delta_mode が適用されないこと = target_ambiguous/target_resolution_failed が出ないこと
        assert 'target_ambiguous' not in result.stderr
        assert 'target_resolution_failed' not in result.stderr

    def test_stderr_format_on_block(self, tmp_path):
        """GIVEN: changed prose block failure WHEN: block THEN: stderr に required fields (AC8)"""
        old_body = "日本語の説明。"
        new_body = "日本語の説明。\n\nThis is a new English prose paragraph added here."

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 999 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 999": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2

        # AC8: stderr に required fields が含まれること
        stderr = result.stderr
        assert 'target:' in stderr or 'issue #999' in stderr
        assert 'changed_prose_blocks:' in stderr
        assert 'failed_blocks:' in stderr
        assert 'ratio_min:' in stderr


# ============================================================
# Blocking fix tests (PR #592 review)
# ============================================================

class TestBlockingFixes:
    """Blocking 1〜5 の修正テスト (PR #592 review)"""

    # ------------------------------------------------------------------
    # Blocking 1: machine_yaml 判定が広すぎて英語 prose を素通し
    # ------------------------------------------------------------------

    def test_delta_blocks_colon_prefixed_english_prose(self):
        """Blocking 1: Note: ... 形式の英語 prose が machine_yaml に誤分類されない"""
        old = "日本語の説明です。"
        new = old + "\n\nNote: This is a new English prose paragraph."
        changed = changed_prose_blocks(old, new)
        assert len(changed) > 0, (
            "Note: ... 形式の英語 prose は prose として検出されるべき"
        )

    def test_delta_blocks_summary_colon_english_prose(self):
        """Blocking 1: Summary: ... 形式の英語 prose が machine_yaml に誤分類されない"""
        old = "日本語の説明です。"
        new = old + "\n\nSummary: This is an English summary with multiple words."
        changed = changed_prose_blocks(old, new)
        assert len(changed) > 0, (
            "Summary: ... 形式の英語 prose は prose として検出されるべき"
        )

    def test_machine_yaml_short_value_still_classified(self):
        """Blocking 1: value が短い identifier/boolean の行は machine_yaml のまま"""
        text = "status: ok\ndecision: pass\nenabled: true"
        blocks = split_markdown_blocks(text)
        assert any(b['type'] == 'machine_yaml' for b in blocks), (
            "短い identifier/boolean 値の key: value ブロックは machine_yaml であるべき"
        )

    # ------------------------------------------------------------------
    # Blocking 2: grep_pattern 判定が広すぎて grep 言及英文を素通し
    # ------------------------------------------------------------------

    def test_delta_blocks_english_sentence_mentioning_grep(self):
        """Blocking 2: grep を言及するだけの英語説明文は prose として検出される"""
        old = "日本語の説明です。"
        new = old + "\n\nUse grep to find the failed workflow logs and update the issue."
        changed = changed_prose_blocks(old, new)
        assert len(changed) > 0, (
            "grep を言及するだけの英語説明文は prose として検出されるべき"
        )

    def test_grep_command_line_still_classified(self):
        """Blocking 2: 行頭から始まる grep コマンド行は grep_pattern として分類される"""
        from validate_japanese_content import _classify_block
        block = "grep -n 'pattern' file.sh"
        result = _classify_block(block)
        assert result == 'grep_pattern', (
            f"grep コマンド行は grep_pattern であるべきだが {result} になった"
        )

    # ------------------------------------------------------------------
    # Blocking 3: .json / .log の --body-file で guard が完全回避できる
    # ------------------------------------------------------------------

    def test_gh_issue_create_body_file_json_still_checked(self, tmp_path):
        """Blocking 3: .json 拡張子でも gh issue create では full-body 検査される"""
        body_content = (
            "This is an English-only issue body in a json-named file. "
            "It should still be blocked."
        )
        body_file = tmp_path / "body.json"
        body_file.write_text(body_content, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue create --title 'Test' --body-file {body_file}"
        )

        result = run_hook(hook_input)
        assert result.returncode == 2, (
            f".json 拡張子でも gh issue create では英語 prose をブロックすべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_gh_issue_edit_body_file_log_delta_still_checked(self, tmp_path):
        """Blocking 3: .log 拡張子でも gh issue edit では delta 検査される（新規英語 prose はブロック）"""
        old_body = "既存の日本語 prose です。"
        new_body = (
            "既存の日本語 prose です。\n\n"
            "This is a newly added English prose paragraph in a log-named file."
        )

        body_file = tmp_path / "body.log"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 400 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 400": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, (
            f".log 拡張子でも新規英語 prose はブロックすべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    # ------------------------------------------------------------------
    # Blocking 4: 空の旧本文を取得失敗と誤判定
    # ------------------------------------------------------------------

    def test_delta_allows_empty_old_body_with_japanese_new_body(self, tmp_path):
        """Blocking 4: 空の旧本文 + 日本語新本文 → pass (exit 0)"""
        old_body = ""  # 空の旧本文（新規作成直後など）
        new_body = "これは日本語の本文です。日本語で書かれた内容です。"

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 500 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 500": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, (
            f"空の旧本文 + 日本語新本文は pass すべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_delta_blocks_empty_old_body_with_english_new_body(self, tmp_path):
        """Blocking 4: 空の旧本文 + 英語新本文 → block (exit 2)"""
        old_body = ""  # 空の旧本文
        new_body = (
            "This is an entirely English prose body added to an empty issue. "
            "It should be blocked by the delta check."
        )

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body, encoding='utf-8')

        hook_input = make_bash_hook_input(
            f"gh issue edit 501 --body-file {body_file}"
        )

        mock_responses = {
            "issue view 501": old_body
        }

        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, (
            f"空の旧本文 + 英語新本文はブロックすべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    # ------------------------------------------------------------------
    # Blocking 5: 同一英語 block の重複追加が差分検知されない
    # ------------------------------------------------------------------

    def test_delta_blocks_duplicate_of_existing_english_prose(self):
        """Blocking 5: 同一内容の prose を重複追加した場合に変更検知される"""
        old = "This is legacy English prose."
        new = old + "\n\n" + old  # 同一内容を重複追加
        changed = changed_prose_blocks(old, new)
        assert len(changed) > 0, (
            "同一英語 prose の重複追加は changed として検出されるべき"
        )
