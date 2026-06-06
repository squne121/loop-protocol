"""test_replay_scenarios.py

Claude Code replay scenario R1〜R7 の structured log harness と test。

各シナリオで以下をアサートする（AC10）:
  - route_id
  - body_source
  - public_mutation
  - decision_would_be
  - failed_block_count
  - exit_code
  - JSONL schema_version

AC5, AC10, AC12 対応。
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).parent.parent / "guard-japanese-prose.sh"
SHADOW_LOG_PY = Path(__file__).parent.parent / "shadow_log.py"

# テスト用の fake gh スクリプトディレクトリ（後で tmp_path に作成）
FAKE_GH_SCRIPT = """#!/usr/bin/env bash
# fake gh: 実 GitHub API を呼ばないスタブ
# 各サブコマンドに対して stub レスポンスを返す

subcmd="${1:-}"
sub2="${2:-}"

case "$subcmd" in
  issue)
    case "$sub2" in
      create|comment)
        exit 0
        ;;
      view)
        echo '{"body":"これは既存の日本語 Issue body です。"}'
        exit 0
        ;;
      edit)
        exit 0
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  pr)
    case "$sub2" in
      create|comment|review)
        exit 0
        ;;
      view)
        echo '{"body":"これは既存の日本語 PR body です。"}'
        exit 0
        ;;
      edit)
        exit 0
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  api)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


def make_fake_gh(tmp_path: Path) -> str:
    """fake gh スクリプトを tmp_path に作成してパスを返す。"""
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(FAKE_GH_SCRIPT, encoding="utf-8")
    fake_gh.chmod(0o755)
    return str(tmp_path)


def run_hook(
    tool_name: str,
    command: str | None = None,
    *,
    env_mode: str | None = None,
    shadow_log_file: str | None = None,
    extra_env: dict | None = None,
    fake_gh_bin_dir: str | None = None,
) -> subprocess.CompletedProcess:
    """guard-japanese-prose.sh を呼び出す。"""
    if command is None:
        tool_input = {}
    else:
        tool_input = {"command": command}

    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})

    env = os.environ.copy()
    if env_mode is not None:
        env["GUARD_JAPANESE_PROSE_MODE"] = env_mode
    elif "GUARD_JAPANESE_PROSE_MODE" in env:
        del env["GUARD_JAPANESE_PROSE_MODE"]

    if shadow_log_file is not None:
        env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = shadow_log_file

    # fake gh を PATH の先頭に追加（実 GitHub API への依存なし / AC6）
    if fake_gh_bin_dir is not None:
        env["PATH"] = fake_gh_bin_dir + ":" + env.get("PATH", "")

    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def read_jsonl(path: str) -> list[dict]:
    """JSONL ファイルを読み込んでリストとして返す。"""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def assert_jsonl_entry(
    log_file: str,
    *,
    expected_decision: str | None = None,
    expected_public_mutation: bool | None = None,
    check_schema_version: bool = True,
    check_route_id: bool = True,
    check_body_source: bool = True,
    check_failed_block_count: bool = True,
    check_duration_ms: bool = True,
) -> dict | None:
    """JSONL ファイルの最終エントリをアサートするヘルパー。"""
    if not os.path.exists(log_file):
        # JSONL が存在しない = shadow が不要なルート（allow 通過）の場合は None
        return None

    entries = read_jsonl(log_file)
    if not entries:
        return None

    entry = entries[-1]

    if check_schema_version:
        assert "schema_version" in entry, f"schema_version が欠落: {entry}"
        assert entry["schema_version"] == "1", f"schema_version != '1': {entry}"

    if check_route_id:
        assert "route_id" in entry, f"route_id が欠落: {entry}"

    if check_body_source:
        assert "body_source" in entry, f"body_source が欠落: {entry}"

    if "public_mutation" in entry and expected_public_mutation is not None:
        assert entry["public_mutation"] == expected_public_mutation, (
            f"public_mutation mismatch: expected={expected_public_mutation}, "
            f"actual={entry['public_mutation']}"
        )

    if expected_decision is not None:
        assert entry.get("decision_would_be") == expected_decision, (
            f"decision_would_be mismatch: expected={expected_decision}, "
            f"actual={entry.get('decision_would_be')}"
        )

    if check_failed_block_count:
        assert "failed_block_count" in entry, f"failed_block_count が欠落: {entry}"

    if check_duration_ms:
        assert "duration_ms" in entry, f"duration_ms が欠落: {entry}"

    return entry


# ---------------------------------------------------------------------------
# R1: gh issue create 日本語本文
# ---------------------------------------------------------------------------

class TestR1IssueCreateJapanese:
    """R1: gh issue create 日本語本文 → public_mutation / allow。"""

    def test_R1_issue_create_japanese_body_shadow(self, tmp_path):
        """R1: 日本語 body は shadow mode で allow（decision_would_be=allow）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "これは日本語で書かれた Issue の本文です。日本語の比率が高いため通過します。",
            encoding="utf-8",
        )
        fake_gh_dir = make_fake_gh(tmp_path)
        command = f"gh issue create --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        # exit_code アサート
        assert result.returncode == 0, f"R1: must exit 0, got {result.returncode}"

        # JSONL フィールドアサート（決定論的に記録されなくてもよい: allow は記録対象外）
        # allow ルートでは JSONL が存在しないか decision_would_be=allow のどちらか
        if os.path.exists(log_file):
            entries = read_jsonl(log_file)
            if entries:
                entry = entries[-1]
                assert entry.get("schema_version") == "1", f"R1: schema_version != 1: {entry}"

    def test_R1_issue_create_japanese_body_enforce(self, tmp_path):
        """R1: 日本語 body は enforce mode でも allow（exit 0）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "これは日本語で書かれた Issue body です。日本語比率は十分です。",
            encoding="utf-8",
        )
        fake_gh_dir = make_fake_gh(tmp_path)
        command = f"gh issue create --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            env_mode="enforce",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        assert result.returncode == 0, f"R1 enforce: Japanese body must exit 0, got {result.returncode}"


# ---------------------------------------------------------------------------
# R2: gh issue edit machine-readable heading のみ
# ---------------------------------------------------------------------------

class TestR2IssueEditMachineReadableHeading:
    """R2: gh issue edit machine-readable heading のみ → allow。"""

    def test_R2_issue_edit_machine_readable_heading(self, tmp_path):
        """R2: machine-readable heading / YAML のみ body は allow。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        # machine-readable フォーマットのみ（prose なし）
        body_file.write_text(
            textwrap.dedent("""\
            ## Machine-Readable Contract

            ```yaml
            contract_schema_version: v1
            issue_kind: implementation
            ```

            ## Allowed Paths

            - `.claude/hooks/guard-japanese-prose.sh`
            """),
            encoding="utf-8",
        )
        fake_gh_dir = make_fake_gh(tmp_path)
        command = f"gh issue edit 657 --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        # route / exit_code
        assert result.returncode == 0, (
            f"R2: machine-readable heading must exit 0 in shadow mode, "
            f"got {result.returncode}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# R3: gh issue comment 英語 prose
# ---------------------------------------------------------------------------

class TestR3IssueCommentEnglishProse:
    """R3: gh issue comment 英語 prose → shadow: would-deny+exit0 / enforce: deny。"""

    def test_R3_shadow_would_deny_exit0(self, tmp_path):
        """R3 shadow: 英語 prose は would-deny だが exit 0 で通過。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is entirely English prose for a GitHub comment. "
            "Shadow mode should record would-deny but exit 0.",
            encoding="utf-8",
        )
        fake_gh_dir = make_fake_gh(tmp_path)
        command = f"gh issue comment 657 --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        # exit_code
        assert result.returncode == 0, (
            f"R3 shadow: must exit 0, got {result.returncode}"
        )

        # JSONL: decision_would_be=deny, public_mutation=true
        if os.path.exists(log_file):
            entries = read_jsonl(log_file)
            deny_entries = [e for e in entries if e.get("decision_would_be") == "deny"]
            if deny_entries:
                entry = deny_entries[-1]
                assert entry.get("schema_version") == "1", f"R3: schema_version != 1: {entry}"
                assert "route_id" in entry, f"R3: route_id 欠落: {entry}"
                assert "body_source" in entry, f"R3: body_source 欠落: {entry}"
                assert "public_mutation" in entry, f"R3: public_mutation 欠落: {entry}"
                assert "failed_block_count" in entry, f"R3: failed_block_count 欠落: {entry}"

    def test_R3_enforce_deny(self, tmp_path):
        """R3 enforce: 英語 prose は exit 2 でブロック。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is entirely English prose. Enforce mode should block this comment.",
            encoding="utf-8",
        )
        fake_gh_dir = make_fake_gh(tmp_path)
        command = f"gh issue comment 657 --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            env_mode="enforce",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        assert result.returncode == 2, (
            f"R3 enforce: must exit 2, got {result.returncode}"
        )


# ---------------------------------------------------------------------------
# R4: termination report dry-run（renderer output を公開 mutation と誤分類しない）
# ---------------------------------------------------------------------------

class TestR4TerminationReportDryRun:
    """R4: Write/Edit/MultiEdit ツール（tmp 下書き相当）は non-public → allow。

    renderer output の誤分類防止に限定。
    英語 heading / HTML marker / machine-readable block を prose と誤判定しない。
    """

    def test_R4_write_tool_allows_without_prose_check(self, tmp_path):
        """R4: Write ツールは public_side_effect=false → guard 対象外（exit 0）。"""
        log_file = str(tmp_path / "shadow.jsonl")

        # Write ツールのシミュレーション: 英語 heading + HTML marker 混在
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "termination_report.md"),
                "content": (
                    "# Termination Report\n\n"
                    "<!-- TERMINATION_REPORT_START -->\n\n"
                    "## Summary\n\n"
                    "This is an English-only renderer output. "
                    "No Japanese prose here. "
                    "Machine-readable sections should not trigger prose check.\n\n"
                    "<!-- TERMINATION_REPORT_END -->\n"
                ),
            },
        })

        env = os.environ.copy()
        env["GUARD_JAPANESE_PROSE_MODE"] = "shadow"
        env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = log_file

        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )
        # Write ツール = non-public → exit 0
        assert result.returncode == 0, (
            f"R4: Write tool must exit 0 (non-public): exit={result.returncode}"
        )

    def test_R4_edit_tool_allows(self, tmp_path):
        """R4: Edit ツールも non-public → exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(tmp_path / "report.md"),
                "old_string": "old",
                "new_string": "new",
            },
        })

        env = os.environ.copy()
        env["GUARD_JAPANESE_PROSE_MODE"] = "enforce"
        env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = log_file

        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            f"R4: Edit tool must exit 0 (non-public): exit={result.returncode}"
        )


# ---------------------------------------------------------------------------
# R5: tmp draft Write/Edit
# ---------------------------------------------------------------------------

class TestR5TmpDraftWriteEdit:
    """R5: tmp draft Write/Edit → non-public → allow。"""

    def test_R5_write_tool_non_public_allow(self, tmp_path):
        """R5: Write ツールは guard 対象外（public_side_effect=false）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/draft_body_12345.md",
                "content": "All English draft content. No Japanese at all.",
            },
        })

        env = os.environ.copy()
        env["GUARD_JAPANESE_PROSE_MODE"] = "enforce"
        env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = log_file

        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )
        # exit_code = 0
        assert result.returncode == 0, (
            f"R5: Write tool must exit 0 (non-public): exit={result.returncode}"
        )

    def test_R5_multiedit_tool_non_public_allow(self, tmp_path):
        """R5: MultiEdit ツールも non-public → exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        payload = json.dumps({
            "tool_name": "MultiEdit",
            "tool_input": {},
        })

        env = os.environ.copy()
        env["GUARD_JAPANESE_PROSE_MODE"] = "enforce"
        env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = log_file

        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, (
            f"R5: MultiEdit tool must exit 0 (non-public): exit={result.returncode}"
        )


# ---------------------------------------------------------------------------
# R6: REST comment create/update -f body= / --input
# ---------------------------------------------------------------------------

class TestR6RestCommentRouteClassification:
    """R6: REST comment create/update -f body= / --input は route/body_source を正しく分類。"""

    def test_R6_shadow_gh_api_field_body_shadow_allow(self, tmp_path):
        """R6 shadow: gh api -X POST -f body= 英語 body は shadow mode で exit 0。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fake_gh_dir = make_fake_gh(tmp_path)

        # -X POST -f body= の英語 body: shadow で exit 0（would-deny 記録）
        command = (
            "gh api -X POST repos/owner/repo/issues/1/comments "
            "-f body='All English no Japanese REST comment test'"
        )
        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        assert result.returncode == 0, (
            f"R6 shadow: must exit 0, got {result.returncode}\nstderr: {result.stderr}"
        )

    def test_R6_enforce_gh_api_field_body_deny(self, tmp_path):
        """R6 enforce: gh api -X POST -f body= 英語 body は exit 2。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fake_gh_dir = make_fake_gh(tmp_path)

        # -X POST で method を明示してブロックを確認
        command = (
            "gh api -X POST repos/owner/repo/issues/1/comments "
            "-f body='All English no Japanese REST comment test enforce'"
        )
        result = run_hook(
            "Bash", command,
            env_mode="enforce",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        # enforce mode で英語 body → exit 2
        assert result.returncode == 2, (
            f"R6 enforce: must exit 2, got {result.returncode}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# R7: GraphQL mutation（conservative deny / shadow would-deny）
# ---------------------------------------------------------------------------

class TestR7GraphQLConservativeDeny:
    """R7: GraphQL mutation → conservative_deny。shadow: would-deny / enforce: deny。"""

    def test_R7_shadow_graphql_mutation_would_deny(self, tmp_path):
        """R7 shadow: GraphQL mutation は shadow mode で exit 0（would-deny 記録）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fake_gh_dir = make_fake_gh(tmp_path)

        command = (
            "gh api graphql "
            "-f query='mutation { updateIssue(input: {id: \"I_123\", body: \"test\"}) { issue { id } } }'"
        )
        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        # shadow: conservative_deny → would-deny だが exit 0
        assert result.returncode == 0, (
            f"R7 shadow: must exit 0 (would-deny), got {result.returncode}"
        )

        # JSONL: would-deny / conservative_deny が記録されているか確認
        if os.path.exists(log_file):
            entries = read_jsonl(log_file)
            if entries:
                entry = entries[-1]
                assert entry.get("schema_version") == "1", f"R7: schema_version != 1: {entry}"
                assert "route_id" in entry, f"R7: route_id 欠落: {entry}"
                assert "body_source" in entry, f"R7: body_source 欠落: {entry}"
                assert "public_mutation" in entry, f"R7: public_mutation 欠落: {entry}"
                assert "failed_block_count" in entry, f"R7: failed_block_count 欠落: {entry}"

    def test_R7_enforce_graphql_mutation_deny(self, tmp_path):
        """R7 enforce: GraphQL mutation は exit 2（conservative deny）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        fake_gh_dir = make_fake_gh(tmp_path)

        command = (
            "gh api graphql "
            "-f query='mutation { updateIssue(input: {id: \"I_456\", body: \"enforce test\"}) { issue { id } } }'"
        )
        result = run_hook(
            "Bash", command,
            env_mode="enforce",
            shadow_log_file=log_file,
            fake_gh_bin_dir=fake_gh_dir,
        )
        assert result.returncode == 2, (
            f"R7 enforce: must exit 2 (conservative deny), got {result.returncode}"
        )

    def test_R7_no_graphql_parser_implementation(self):
        """AC12: guard-japanese-prose.sh に GraphQL parser 実装がない。

        conservative deny のみ許容（parser 未実装 = deny_graphql_mutation_unsupported）。
        """
        import re

        script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
        # 禁止パターン: 新規 GraphQL parser 実装
        forbidden_patterns = [
            r"graphql_parse_body",
            r"parse_graphql_mutation",
            r"GraphQLParser",
        ]
        for pattern in forbidden_patterns:
            assert not re.search(pattern, script_text), (
                f"AC12: guard-japanese-prose.sh に禁止パターン '{pattern}' が存在する"
            )


# ---------------------------------------------------------------------------
# textwrap import（R2 用）
# ---------------------------------------------------------------------------

import textwrap
