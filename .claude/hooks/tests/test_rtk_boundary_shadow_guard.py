"""test_rtk_boundary_shadow_guard.py

rtk_boundary_shadow_guard.sh の単体テスト。

AC3 対応: hook が常に exit 0 で終了することを検証する。
  - risky 分類
  - read-only 非分類
  - 不正 JSON 入力時の fail-open
  - malformed stdin
  - unwritable log
  - empty stdin
  - missing jq (モック)

fixture matrix:
  git status           -> safe_readonly_git
  git log --oneline -5 -> safe_readonly_git
  git commit -m x      -> mutating_git
  git push origin HEAD -> mutating_git
  git reset --hard HEAD~1 -> mutating_git
  git rebase main      -> mutating_git
  gh issue view 823    -> safe_readonly_gh
  gh issue edit 823 --body-file tmp/x.md -> mutating_gh
  gh pr create --fill  -> mutating_gh
  gh api repos/x/y/issues/1 -X PATCH -f body=x -> mutating_gh_api
  pnpm test            -> safe_validation
  pnpm add lodash      -> dependency_mutation
  npm install          -> dependency_mutation
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HOOK_SH = Path(__file__).parent.parent / "rtk_boundary_shadow_guard.sh"


def run_hook(
    command: str,
    tool_name: str = "Bash",
    tool_use_id: str = "test-tool-use-id",
    env_override: dict | None = None,
    stdin_raw: str | None = None,
) -> subprocess.CompletedProcess:
    """rtk_boundary_shadow_guard.sh を実行するヘルパー。"""
    if stdin_raw is not None:
        input_text = stdin_raw
    else:
        payload = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "tool_input": {"command": command},
        }
        input_text = json.dumps(payload)

    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    return subprocess.run(
        ["bash", str(HOOK_SH)],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )


def read_jsonl(path: str) -> list[dict]:
    """JSONL ファイルを読み込んでリストとして返す。"""
    entries = []
    p = Path(path)
    if not p.exists():
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ============================================================
# 常に exit 0 であることを確認するテスト（AC3 直接対応）
# ============================================================

class TestAlwaysExit0:
    """AC3: あらゆる状況で exit 0 を返すことを検証する。"""

    def test_exit0_risky_git_commit(self, tmp_path):
        """mutating_git (git commit) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("git commit -m x", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0, f"exit code must be 0, got {result.returncode}\nstderr: {result.stderr}"
        assert result.stdout == "", f"stdout must be empty, got: {result.stdout!r}"

    def test_exit0_risky_git_push(self, tmp_path):
        """mutating_git (git push) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("git push origin HEAD", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_risky_gh_issue_edit(self, tmp_path):
        """mutating_gh (gh issue edit) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("gh issue edit 823 --body-file tmp/x.md", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_risky_gh_pr_create(self, tmp_path):
        """mutating_gh (gh pr create) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("gh pr create --fill", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_risky_pnpm_add(self, tmp_path):
        """dependency_mutation (pnpm add) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("pnpm add lodash", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_risky_npm_install(self, tmp_path):
        """dependency_mutation (npm install) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("npm install", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_readonly_git_status(self, tmp_path):
        """safe_readonly_git (git status) で exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("git status", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_readonly_pnpm_test(self, tmp_path):
        """safe_validation (pnpm test) で exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("pnpm test", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_empty_stdin(self, tmp_path):
        """empty stdin でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("", stdin_raw="", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_malformed_stdin_not_json(self, tmp_path):
        """malformed stdin (not JSON) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("", stdin_raw="not-valid-json{{{", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_malformed_stdin_partial_json(self, tmp_path):
        """malformed stdin (partial JSON) でも exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("", stdin_raw='{"tool_name": "Bash"', env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_unwritable_log(self, tmp_path):
        """unwritable log でも exit 0（ログ書き込み失敗は warn のみ）。"""
        unwritable_log = "/nonexistent_dir_rtk_xyz_abc/shadow.jsonl"
        result = run_hook("git commit -m x", env_override={"RTK_SHADOW_LOG": unwritable_log})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_non_bash_tool(self, tmp_path):
        """Bash 以外のツール (Read) でも exit 0 (スコープ外)。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("some-command", tool_name="Read", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit0_missing_jq(self, tmp_path):
        """PATH から jq を取り除いた状態でも exit 0 (fail-open)。"""
        import shutil
        log_file = str(tmp_path / "shadow.jsonl")
        # fake_bin ディレクトリに jq 以外の必要コマンドをコピーして PATH に追加
        fake_bin = tmp_path / "fake_bin"
        fake_bin.mkdir()

        # bash / cat / awk / sed / sha256sum / date / dirname / mkdir / head は必要
        # jq だけ含めない PATH を作る:
        # fake_bin を先頭にして、jq だけ除外したラッパーを使う
        # 最も簡単な方法: fake_bin に "jq" という名前で終了コード 127 を返すスタブを置く
        # これで "command -v jq" が成功しても実際には失敗する... ではなく
        # "command -v jq" が失敗するように fake_bin に jq を含めず、通常の PATH を継続する
        #
        # 実際の環境: jq が /usr/bin/jq にある場合、fake_bin を先頭に置いても
        # PATH に /usr/bin が残っていれば jq が見つかる。
        # jq を本当に除外するには:
        # 1) PATH を fake_bin のみにして bash も入れる、または
        # 2) jq だけ壊れたスタブで上書きする。方法 2 を採用。
        stub_jq = fake_bin / "jq"
        stub_jq.write_text("#!/bin/bash\nexit 127\n")
        stub_jq.chmod(0o755)

        # fake_bin を先頭に置き、bash / awk / sed 等は通常の PATH から継承
        original_path = os.environ.get("PATH", "/usr/bin:/bin")
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + ":" + original_path
        env["RTK_SHADOW_LOG"] = log_file

        result = subprocess.run(
            ["bash", str(HOOK_SH)],
            input=json.dumps({
                "tool_name": "Bash",
                "tool_use_id": "test-id",
                "tool_input": {"command": "git commit -m x"},
            }),
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"missing jq must still exit 0\nstderr: {result.stderr}"
        assert result.stdout == ""


# ============================================================
# fixture matrix テスト（分類ロジックの検証）
# ============================================================

class TestClassificationMatrix:
    """分類カテゴリの fixture matrix テスト。"""

    def _get_category(self, command: str, tmp_path) -> str:
        """コマンドを実行して JSONL から category を取得する。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook(command, env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0, f"exit code must be 0\nstderr: {result.stderr}"
        entries = read_jsonl(log_file)
        if not entries:
            return "no_entry"
        return entries[-1].get("category", "missing_category")

    def test_git_status_is_safe_readonly_git(self, tmp_path):
        assert self._get_category("git status", tmp_path) == "safe_readonly_git"

    def test_git_log_is_safe_readonly_git(self, tmp_path):
        assert self._get_category("git log --oneline -5", tmp_path) == "safe_readonly_git"

    def test_git_commit_is_mutating_git(self, tmp_path):
        assert self._get_category("git commit -m x", tmp_path) == "mutating_git"

    def test_git_push_is_mutating_git(self, tmp_path):
        assert self._get_category("git push origin HEAD", tmp_path) == "mutating_git"

    def test_git_reset_is_mutating_git(self, tmp_path):
        assert self._get_category("git reset --hard HEAD~1", tmp_path) == "mutating_git"

    def test_git_rebase_is_mutating_git(self, tmp_path):
        assert self._get_category("git rebase main", tmp_path) == "mutating_git"

    def test_gh_issue_view_is_safe_readonly_gh(self, tmp_path):
        assert self._get_category("gh issue view 823", tmp_path) == "safe_readonly_gh"

    def test_gh_issue_edit_is_mutating_gh(self, tmp_path):
        assert self._get_category("gh issue edit 823 --body-file tmp/x.md", tmp_path) == "mutating_gh"

    def test_gh_pr_create_is_mutating_gh(self, tmp_path):
        assert self._get_category("gh pr create --fill", tmp_path) == "mutating_gh"

    def test_gh_api_patch_is_mutating_gh_api(self, tmp_path):
        assert self._get_category("gh api repos/x/y/issues/1 -X PATCH -f body=x", tmp_path) == "mutating_gh_api"

    def test_pnpm_test_is_safe_validation(self, tmp_path):
        assert self._get_category("pnpm test", tmp_path) == "safe_validation"

    def test_pnpm_add_is_dependency_mutation(self, tmp_path):
        assert self._get_category("pnpm add lodash", tmp_path) == "dependency_mutation"

    def test_npm_install_is_dependency_mutation(self, tmp_path):
        assert self._get_category("npm install", tmp_path) == "dependency_mutation"


# ============================================================
# JSONL スキーマ検証テスト
# ============================================================

class TestJsonlSchema:
    """JSONL に記録されるフィールドのスキーマ検証。"""

    def test_required_fields_present_for_mutating_git(self, tmp_path):
        """mutating_git の JSONL エントリに必須フィールドが含まれる。"""
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook("git commit -m x", env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        assert len(entries) >= 1
        entry = entries[-1]

        required_fields = [
            "guard_name",
            "category",
            "matched_rule",
            "decision_would_be",
            "command_sha256",
            "command_preview_redacted",
            "command_bytes",
            "session_id",
            "tool_use_id",
            "timestamp",
        ]
        for field in required_fields:
            assert field in entry, f"required field '{field}' missing from JSONL entry"

    def test_guard_name_is_correct(self, tmp_path):
        """guard_name が rtk_boundary_shadow_guard。"""
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook("git push origin HEAD", env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        assert entries[-1]["guard_name"] == "rtk_boundary_shadow_guard"

    def test_no_raw_command_in_jsonl(self, tmp_path):
        """JSONL に raw command / full_command / command_line が保存されない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook("git commit -m secret_message", env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        assert len(entries) >= 1
        entry = entries[-1]

        forbidden_keys = ["full_command", "command_line", "command", "raw_command"]
        for key in forbidden_keys:
            assert key not in entry, f"forbidden field '{key}' found in JSONL entry"

    def test_command_sha256_format(self, tmp_path):
        """command_sha256 が sha256: プレフィックスを持つ。"""
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook("git push origin HEAD", env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        sha = entries[-1].get("command_sha256", "")
        assert sha.startswith("sha256:"), f"command_sha256 must start with 'sha256:', got: {sha!r}"

    def test_command_preview_redacted_max_200_bytes(self, tmp_path):
        """command_preview_redacted が 200 bytes 以内。"""
        # 200 bytes を超える長いコマンド
        long_cmd = "git commit -m " + "x" * 300
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook(long_cmd, env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        if entries:
            preview = entries[-1].get("command_preview_redacted", "")
            assert len(preview.encode("utf-8")) <= 200, (
                f"command_preview_redacted must be <= 200 bytes, got {len(preview.encode('utf-8'))}"
            )

    def test_gh_token_redacted_in_preview(self, tmp_path):
        """GH_TOKEN が command_preview_redacted で redact される。"""
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook("gh issue edit 1 GH_TOKEN=secret123", env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        if entries:
            preview = entries[-1].get("command_preview_redacted", "")
            assert "secret123" not in preview, f"GH_TOKEN value leaked in preview: {preview!r}"

    def test_decision_would_be_deny_for_mutating(self, tmp_path):
        """mutating コマンドは decision_would_be が deny。"""
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook("git push origin HEAD", env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        assert entries[-1]["decision_would_be"] == "deny"

    def test_decision_would_be_allow_for_readonly(self, tmp_path):
        """readonly コマンドは decision_would_be が allow。"""
        log_file = str(tmp_path / "shadow.jsonl")
        run_hook("git status", env_override={"RTK_SHADOW_LOG": log_file})
        entries = read_jsonl(log_file)
        # safe_readonly は記録される（category が safe_readonly_git）
        if entries:
            assert entries[-1]["decision_would_be"] == "allow"

    def test_stdout_is_always_empty(self, tmp_path):
        """hook の stdout は常に空。"""
        log_file = str(tmp_path / "shadow.jsonl")
        for cmd in [
            "git commit -m x",
            "git status",
            "gh issue edit 1 --body x",
            "pnpm add lodash",
            "pnpm test",
        ]:
            result = run_hook(cmd, env_override={"RTK_SHADOW_LOG": log_file})
            assert result.stdout == "", f"stdout must be empty for command '{cmd}', got: {result.stdout!r}"


# ============================================================
# Fix Delta 1: gh api field flags → mutating_gh_api
# ============================================================

class TestGhApiFieldFlags:
    """gh api の -f/-F/--raw-field/--field/--input が implicit POST になることを検証。"""

    def _get_category(self, command: str, tmp_path) -> str:
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook(command, env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        entries = read_jsonl(log_file)
        if not entries:
            return "no_entry"
        return entries[-1].get("category", "missing_category")

    def test_gh_api_raw_field_defaults_to_post_is_mutating_gh_api(self, tmp_path):
        # gh api repos/x/y/issues/1/comments -f body=x → mutating_gh_api
        assert self._get_category(
            "gh api repos/x/y/issues/1/comments -f body=x", tmp_path
        ) == "mutating_gh_api"

    def test_gh_api_field_flag_defaults_to_post_is_mutating_gh_api(self, tmp_path):
        # gh api repos/x/y/issues/1 -F title=x → mutating_gh_api
        assert self._get_category(
            "gh api repos/x/y/issues/1 -F title=x", tmp_path
        ) == "mutating_gh_api"

    def test_gh_api_input_flag_is_mutating_gh_api(self, tmp_path):
        # gh api repos/x/y/issues/1 --input /tmp/body.json → mutating_gh_api
        assert self._get_category(
            "gh api repos/x/y/issues/1 --input /tmp/body.json", tmp_path
        ) == "mutating_gh_api"

    def test_gh_api_explicit_get_with_field_is_readonly(self, tmp_path):
        # gh api repos/x/y/issues/1 --method GET -f per_page=10 → safe_readonly_gh (GET は安全)
        assert self._get_category(
            "gh api repos/x/y/issues/1 --method GET -f per_page=10", tmp_path
        ) == "safe_readonly_gh"

    def test_gh_api_no_field_flags_is_readonly(self, tmp_path):
        # gh api repos/x/y/issues → safe_readonly_gh（フィールドフラグなし）
        assert self._get_category(
            "gh api repos/x/y/issues", tmp_path
        ) == "safe_readonly_gh"


# ============================================================
# Fix Delta 2: Authorization header redaction
# ============================================================

class TestAuthSecretsRedacted:
    """Authorization ヘッダー等の機密情報が command_preview_redacted に含まれないことを検証。"""

    def _get_preview(self, command: str, tmp_path) -> str:
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook(command, env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        entries = read_jsonl(log_file)
        if not entries:
            return ""
        return entries[-1].get("command_preview_redacted", "")

    @pytest.mark.parametrize("cmd", [
        "gh api /user -H 'Authorization: Bearer SECRET123'",
        'gh api /user --header "Authorization: token SECRET123"',
        "gh api '/repos/x/y/issues?access_token=SECRET123'",
    ])
    def test_auth_secrets_redacted(self, cmd, tmp_path):
        # command_preview_redacted に SECRET123 が含まれないこと
        preview = self._get_preview(cmd, tmp_path)
        assert "SECRET123" not in preview, (
            f"Secret leaked in command_preview_redacted: {preview!r}"
        )

    def test_bearer_token_redacted(self, tmp_path):
        preview = self._get_preview(
            "gh api /user -H 'Authorization: Bearer ghp_ABCDEFG123456'", tmp_path
        )
        assert "ghp_ABCDEFG123456" not in preview

    def test_access_token_url_query_redacted(self, tmp_path):
        preview = self._get_preview(
            "gh api '/repos/x/y?access_token=mytoken123'", tmp_path
        )
        assert "mytoken123" not in preview

    def test_redact_before_truncation(self, tmp_path):
        """truncation 前に full command に対して redact が行われることを確認。
        機密情報が 200 bytes 内に収まる位置にある場合でも必ず redact される。"""
        # SECRET が最初の 200 bytes 内に来るよう短めのコマンドを使う
        cmd = "gh api /repos/x/y -H 'Authorization: Bearer SHORTTOKEN'"
        preview = self._get_preview(cmd, tmp_path)
        assert "SHORTTOKEN" not in preview, (
            f"Token should be redacted before truncation, but found in: {preview!r}"
        )


# ============================================================
# Fix Delta 3: wrapper/bypass パターンのテスト
# ============================================================

class TestWrapperBypassPatterns:
    """git -C / bash -lc / command git push 等の wrapper パターンを検証。"""

    def _get_category(self, command: str, tmp_path) -> str:
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook(command, env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        entries = read_jsonl(log_file)
        if not entries:
            return "no_entry"
        return entries[-1].get("category", "missing_category")

    def _get_decision(self, command: str, tmp_path) -> str:
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook(command, env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        entries = read_jsonl(log_file)
        if not entries:
            return "no_entry"
        return entries[-1].get("decision_would_be", "no_entry")

    def test_git_with_dash_C_push_is_mutating_git(self, tmp_path):
        # git -C ../repo push → mutating_git
        assert self._get_category("git -C ../repo push", tmp_path) == "mutating_git"

    def test_git_with_dash_C_commit_is_mutating_git(self, tmp_path):
        # git -C /path/to/repo commit -m msg → mutating_git
        assert self._get_category("git -C /path/to/repo commit -m msg", tmp_path) == "mutating_git"

    def test_git_with_dash_C_status_is_readonly(self, tmp_path):
        # git -C ../repo status → safe_readonly_git
        assert self._get_category("git -C ../repo status", tmp_path) == "safe_readonly_git"

    def test_bash_lc_git_push_is_out_of_scope_or_risky(self, tmp_path):
        # bash -lc 'git push origin HEAD' → out_of_scope_logged または mutating_git
        # decision_would_be: deny 側に倒す
        category = self._get_category("bash -lc 'git push origin HEAD'", tmp_path)
        assert category in ("mutating_git", "out_of_scope_logged"), (
            f"bash -lc 'git push' should be mutating_git or out_of_scope_logged, got: {category!r}"
        )

    def test_command_git_push_is_mutating_git(self, tmp_path):
        # command git push → mutating_git または out_of_scope_logged
        category = self._get_category("command git push", tmp_path)
        assert category in ("mutating_git", "out_of_scope_logged"), (
            f"'command git push' should be mutating_git or out_of_scope_logged, got: {category!r}"
        )


# ============================================================
# Fix Delta 4: checkout/switch/restore/stash/worktree の分類
# ============================================================

class TestMutatingGitSubcommands:
    """checkout / switch / restore / stash push / worktree add が mutating_git に分類されることを検証。"""

    def _get_category(self, command: str, tmp_path) -> str:
        log_file = str(tmp_path / "shadow.jsonl")
        result = run_hook(command, env_override={"RTK_SHADOW_LOG": log_file})
        assert result.returncode == 0
        entries = read_jsonl(log_file)
        if not entries:
            return "no_entry"
        return entries[-1].get("category", "missing_category")

    def test_git_checkout_branch_is_mutating(self, tmp_path):
        # git checkout main → mutating_git（branch 切り替えは mutation）
        assert self._get_category("git checkout main", tmp_path) == "mutating_git"

    def test_git_switch_branch_is_mutating(self, tmp_path):
        # git switch main → mutating_git
        assert self._get_category("git switch main", tmp_path) == "mutating_git"

    def test_git_restore_file_is_mutating(self, tmp_path):
        # git restore src/foo.ts → mutating_git（ファイル上書き）
        assert self._get_category("git restore src/foo.ts", tmp_path) == "mutating_git"

    def test_git_stash_push_is_mutating(self, tmp_path):
        # git stash push → mutating_git
        assert self._get_category("git stash push", tmp_path) == "mutating_git"

    def test_git_stash_bare_is_mutating(self, tmp_path):
        # git stash（サブコマンドなし）→ mutating_git
        assert self._get_category("git stash", tmp_path) == "mutating_git"

    def test_git_stash_list_is_readonly(self, tmp_path):
        # git stash list → safe_readonly_git
        assert self._get_category("git stash list", tmp_path) == "safe_readonly_git"

    def test_git_stash_show_is_readonly(self, tmp_path):
        # git stash show → safe_readonly_git
        assert self._get_category("git stash show", tmp_path) == "safe_readonly_git"

    def test_git_worktree_add_is_mutating(self, tmp_path):
        # git worktree add .claude/worktrees/foo → mutating_git
        assert self._get_category("git worktree add .claude/worktrees/foo", tmp_path) == "mutating_git"

    def test_git_worktree_list_is_readonly(self, tmp_path):
        # git worktree list → safe_readonly_git
        assert self._get_category("git worktree list", tmp_path) == "safe_readonly_git"

    def test_git_worktree_remove_is_mutating(self, tmp_path):
        # git worktree remove .claude/worktrees/foo → mutating_git
        assert self._get_category("git worktree remove .claude/worktrees/foo", tmp_path) == "mutating_git"
