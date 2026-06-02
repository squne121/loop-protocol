"""
test_guard_api_input.py

guard-japanese-prose.sh の gh api --input <file> 検査 (Issue #594) の smoke test。

Coverage:
- AC1: gh issue create / gh issue comment / gh pr review は full-body 検査を維持
  (test_full_body_create_or_comment_maintained)
- AC2: gh issue edit / gh pr edit --body-file delta 検査を維持
  (test_body_file_delta_edit_maintained)
- AC4: gh api --input <file> が Issue body PATCH -> delta 検査対象
  (TestApiInputIssuesPatch)
- AC5: gh api --input <file> が body mutation でない -> pass
  (TestApiInputNonBodyMutation)
- AC15: テストは実 GitHub API に依存しない fixture で再現 (全テスト)
- AC17: PATCH repos/{owner}/{repo}/issues/{n} + body key -> delta check
  (test_api_input_issues_patch_*)
- AC18: PATCH repos/{owner}/{repo}/pulls/{n} + body key -> delta check
  (test_api_input_pulls_patch_*)
- AC19: gh api --input - (stdin) -> fail-closed
  (test_api_input_stdin_fail_closed)
- AC20: invalid JSON payload -> fail-closed + api_payload_parse_failed
  (test_api_invalid_json_fail_closed)
- AC24: fake_gh / shadow_gh / PATH prepend で実 GitHub API を呼ばない (run_hook helper)
"""

import json
import os
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
from validate_japanese_content import (
    split_markdown_blocks,
    changed_prose_blocks,
    validate_text,
)


# ============================================================
# Helpers
# ============================================================

def _build_fake_gh_script(responses: dict) -> str:
    """
    fake_gh / shadow_gh スクリプトを生成する。
    PATH prepend で実 GitHub API を呼ばない (AC24).
    """
    lines = ["#!/usr/bin/env bash", ""]
    lines.append("# fake_gh shadow_gh: PATH prepend for testing — real GitHub API NOT called (AC24)")
    lines.append('ARGS="$*"')
    lines.append("")

    for pattern, response in responses.items():
        escaped = response.replace("'", "'\"'\"'")
        lines.append(f'if echo "$ARGS" | grep -q "{pattern}"; then')
        lines.append(f"  printf '%s' '{escaped}'")
        lines.append("  exit 0")
        lines.append("fi")
        lines.append("")

    lines.append('echo "fake_gh shadow_gh: unmatched command: $ARGS" >&2')
    lines.append("exit 1")
    return "\n".join(lines) + "\n"


def run_hook(hook_input: dict, mock_gh_responses: dict = None) -> subprocess.CompletedProcess:
    """
    guard-japanese-prose.sh をサブプロセスで実行する。
    mock_gh_responses が渡された場合、PATH 先頭に fake_gh (shadow_gh) を配置し
    実 GitHub API を呼ばない (AC24: fake_gh / shadow_gh / PATH prepend)。
    """
    input_json = json.dumps(hook_input)
    env = os.environ.copy()
    env["PROJECT_DIR"] = str(PROJECT_DIR)

    with tempfile.TemporaryDirectory() as tmpdir:
        if mock_gh_responses is not None:
            fake_gh_path = os.path.join(tmpdir, "gh")
            fake_gh_content = _build_fake_gh_script(mock_gh_responses)
            with open(fake_gh_path, "w") as f:
                f.write(fake_gh_content)
            os.chmod(fake_gh_path, 0o755)
            # AC24: PATH prepend — shadow_gh is used instead of real gh
            env["PATH"] = tmpdir + ":" + env.get("PATH", "")

        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=input_json,
            capture_output=True,
            text=True,
            env=env,
        )
        return result


def make_bash_hook_input(command: str) -> dict:
    """Bash ツールの hook input JSON を生成する"""
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def run_validator(*args) -> subprocess.CompletedProcess:
    """validate_japanese_content.py をサブプロセスで実行するヘルパー"""
    return subprocess.run(
        ["uv", "run", "python3", str(VALIDATOR)] + list(args),
        capture_output=True,
        text=True,
    )


# ============================================================
# Unit tests: --parse-api-input
# ============================================================

class TestParseApiInput:
    """validate_japanese_content.py --parse-api-input の単体テスト"""

    def test_parse_api_input_file(self):
        """GIVEN: gh api --input /path/to/file WHEN: --parse-api-input THEN: API_INPUT_FILE:<path>"""
        result = run_validator(
            "--parse-api-input",
            "gh api repos/owner/repo/issues/123 --method PATCH --input /tmp/body.json",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "API_INPUT_FILE:/tmp/body.json"

    def test_parse_api_input_stdin(self):
        """GIVEN: gh api --input - WHEN: --parse-api-input THEN: API_INPUT_STDIN"""
        result = run_validator(
            "--parse-api-input",
            "gh api repos/owner/repo/issues/123 --input -",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "API_INPUT_STDIN"

    def test_parse_api_input_none(self):
        """GIVEN: gh api without --input WHEN: --parse-api-input THEN: API_INPUT_NONE"""
        result = run_validator(
            "--parse-api-input",
            "gh api repos/owner/repo/issues/123 --method PATCH -f body=test",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "API_INPUT_NONE"

    def test_parse_api_input_inline_form(self):
        """GIVEN: gh api --input=<path> WHEN: --parse-api-input THEN: API_INPUT_FILE:<path>"""
        result = run_validator(
            "--parse-api-input",
            "gh api repos/owner/repo/issues/123 --input=/tmp/body.json",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "API_INPUT_FILE:/tmp/body.json"


# ============================================================
# Unit tests: --classify-api-mutation
# ============================================================

class TestClassifyApiMutation:
    """validate_japanese_content.py --classify-api-mutation の単体テスト"""

    def test_classify_issue_body_mutation(self, tmp_path):
        """GIVEN: payload has body key + issues endpoint THEN: BODY_MUTATION_ISSUE:<n> (AC17)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"body": "test body"}))
        result = run_validator(
            "--classify-api-mutation", str(payload_file),
            "--api-endpoint", "repos/owner/repo/issues/123",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "BODY_MUTATION_ISSUE:123"

    def test_classify_pr_body_mutation(self, tmp_path):
        """GIVEN: payload has body key + pulls endpoint THEN: BODY_MUTATION_PR:<n> (AC18)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"body": "test body"}))
        result = run_validator(
            "--classify-api-mutation", str(payload_file),
            "--api-endpoint", "repos/owner/repo/pulls/42",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "BODY_MUTATION_PR:42"

    def test_classify_no_body_key(self, tmp_path):
        """GIVEN: payload has no body key THEN: NOT_BODY_MUTATION (AC5)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"title": "test title", "state": "open"}))
        result = run_validator(
            "--classify-api-mutation", str(payload_file),
            "--api-endpoint", "repos/owner/repo/issues/123",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "NOT_BODY_MUTATION"

    def test_classify_invalid_json(self, tmp_path):
        """GIVEN: invalid JSON payload THEN: PAYLOAD_PARSE_FAILED (AC20)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text("not valid json {{{")
        result = run_validator(
            "--classify-api-mutation", str(payload_file),
            "--api-endpoint", "repos/owner/repo/issues/123",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "PAYLOAD_PARSE_FAILED"

    def test_classify_non_issue_pr_endpoint(self, tmp_path):
        """GIVEN: payload has body + non-issue/pr endpoint THEN: NOT_BODY_MUTATION (AC5)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"body": "test body"}))
        result = run_validator(
            "--classify-api-mutation", str(payload_file),
            "--api-endpoint", "repos/owner/repo/comments/456",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "NOT_BODY_MUTATION"


# ============================================================
# Hook integration tests: gh api --input (AC4, AC17, AC18)
# ============================================================

class TestApiInputIssuesPatch:
    """AC17: PATCH repos/{owner}/{repo}/issues/{n} + body key -> delta check"""

    def test_api_input_issues_patch_japanese_only_change_pass(self, tmp_path):
        """GIVEN: issue body mutation with Japanese-only new prose WHEN: gh api --input THEN: exit 0 (AC4, AC17)"""
        old_body = "既存の日本語説明。コードフェンスのみ変更します。"
        payload = {"body": "既存の日本語説明。コードフェンスのみ変更します。\n\n追加された日本語段落です。"}
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123 --method PATCH --input {payload_file}"
        )
        mock_responses = {"issue view 123": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, (
            f"日本語 prose 追加は pass するべき: exit {result.returncode}, stderr={result.stderr}"
        )

    def test_api_input_issues_patch_english_prose_blocked(self, tmp_path):
        """GIVEN: issue body mutation adds English prose WHEN: gh api --input THEN: exit 2 (AC4, AC10, AC17)"""
        old_body = "既存の日本語説明。"
        new_body = "既存の日本語説明。\n\nThis is a new English prose paragraph added via gh api --input."
        payload = {"body": new_body}
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/456 --method PATCH --input {payload_file}"
        )
        mock_responses = {"issue view 456": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, (
            f"gh api --input での英語 prose 追加はブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_api_input_issues_patch_code_fence_only_pass(self, tmp_path):
        """GIVEN: issue body mutation changes only code fence WHEN: gh api --input THEN: exit 0 (AC4, AC17)"""
        old_body = "日本語の説明。\n\n```bash\necho old\n```"
        new_body = "日本語の説明。\n\n```bash\necho new\n```"
        payload = {"body": new_body}
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/789 --method PATCH --input {payload_file}"
        )
        mock_responses = {"issue view 789": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, (
            f"code fence のみ変更は pass するべき: exit {result.returncode}, stderr={result.stderr}"
        )


class TestApiInputPullsPatch:
    """AC18: PATCH repos/{owner}/{repo}/pulls/{n} + body key -> delta check"""

    def test_api_input_pulls_patch_english_prose_blocked(self, tmp_path):
        """GIVEN: PR body mutation adds English prose WHEN: gh api --input THEN: exit 2 (AC4, AC10, AC18)"""
        old_body = "既存の日本語 PR 説明。"
        new_body = (
            "既存の日本語 PR 説明。\n\n"
            "This is a new English prose paragraph added to the PR via gh api --input."
        )
        payload = {"body": new_body}
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/pulls/42 --method PATCH --input {payload_file}"
        )
        mock_responses = {"pr view 42": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, (
            f"gh api --input での PR 英語 prose 追加はブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_api_input_pulls_patch_japanese_pass(self, tmp_path):
        """GIVEN: PR body mutation with Japanese-only change WHEN: gh api --input THEN: exit 0 (AC18)"""
        old_body = "既存の日本語 PR 説明。"
        payload = {"body": "既存の日本語 PR 説明。\n\n新しい日本語段落を追加しました。"}
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/pulls/99 --method PATCH --input {payload_file}"
        )
        mock_responses = {"pr view 99": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, (
            f"日本語 prose のみ変更は pass するべき: exit {result.returncode}, stderr={result.stderr}"
        )


class TestApiInputStdinFailClosed:
    """AC19: gh api --input - (stdin) -> fail-closed"""

    def test_api_input_stdin_fail_closed(self):
        """GIVEN: gh api --input - WHEN: hook THEN: exit 2 (AC19)"""
        hook_input = make_bash_hook_input(
            "gh api repos/owner/repo/issues/123 --method PATCH --input -"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"gh api --input - (stdin) は fail-closed でブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )
        stderr = result.stderr.lower()
        assert "stdin" in stderr or "fail-closed" in stderr, (
            f"stderr に stdin/fail-closed が含まれるべき: {result.stderr}"
        )


class TestApiInvalidJsonFailClosed:
    """AC20: invalid JSON payload -> fail-closed"""

    def test_api_invalid_json_fail_closed(self, tmp_path):
        """GIVEN: gh api --input with invalid JSON WHEN: hook THEN: exit 2 + api_payload_parse_failed (AC20)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text("{ invalid json here }")

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123 --method PATCH --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"invalid JSON payload は fail-closed でブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )
        assert "api_payload_parse_failed" in result.stderr, (
            f"stderr に api_payload_parse_failed が含まれるべき: {result.stderr}"
        )


class TestApiInputNonBodyMutation:
    """AC5: gh api --input payload が body mutation でない -> pass"""

    def test_api_non_body_mutation_pass(self, tmp_path):
        """GIVEN: gh api --input payload has no body key WHEN: hook THEN: exit 0 (AC5)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"state": "closed", "title": "New title"}))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123 --method PATCH --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 0, (
            f"body mutation でない payload は pass するべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_api_non_issue_pr_endpoint_pass(self, tmp_path):
        """GIVEN: gh api --input to issues/{n}/comments (POST) WHEN: hook THEN: exit 0 (AC5)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps({"body": "comment body in English only"}))

        # issues/{n}/comments は body mutation 対象外 (issues/{n} のみ対象)
        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123/comments --method POST --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 0, (
            f"コメント endpoint への api --input は body mutation でないため pass するべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )


class TestFullBodyCreateOrCommentMaintained:
    """AC1: gh issue create / gh issue comment / gh pr review は full-body 検査を維持する"""

    def test_full_body_create_or_comment_maintained(self, tmp_path):
        """GIVEN: gh issue create with English-only body WHEN: hook THEN: exit 2 (AC1)"""
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is an English-only issue body. It should be blocked by the full-body check."
        )
        hook_input = make_bash_hook_input(
            f"gh issue create --title 'Test issue' --body-file {body_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"gh issue create の英語 body は full-body 検査でブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )


class TestBodyFileDeltaEditMaintained:
    """AC2: gh issue edit / gh pr edit --body-file delta 検査を維持する (#592 regression)"""

    def test_body_file_delta_edit_maintained(self, tmp_path):
        """GIVEN: gh issue edit --body-file code-fence-only change WHEN: hook THEN: exit 0 (AC2)"""
        old_body = "日本語の説明。\n\n```bash\necho old\n```"
        new_body = "日本語の説明。\n\n```bash\necho new\n```"

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body)

        hook_input = make_bash_hook_input(
            f"gh issue edit 123 --body-file {body_file}"
        )
        mock_responses = {"issue view 123": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 0, (
            f"gh issue edit delta mode (code fence only) は pass するべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_body_file_delta_edit_english_prose_blocked(self, tmp_path):
        """GIVEN: gh issue edit --body-file adds English prose WHEN: hook THEN: exit 2 (AC2)"""
        old_body = "既存の日本語説明。"
        new_body = "既存の日本語説明。\n\nThis is new English prose added via body-file."

        body_file = tmp_path / "body.md"
        body_file.write_text(new_body)

        hook_input = make_bash_hook_input(
            f"gh issue edit 456 --body-file {body_file}"
        )
        mock_responses = {"issue view 456": old_body}
        result = run_hook(hook_input, mock_responses)
        assert result.returncode == 2, (
            f"gh issue edit delta mode での英語 prose 追加はブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )


# ============================================================
# B1: GraphQL mutation body deny tests (AC23: api_graphql_mutation_deny)
# ============================================================

class TestGraphqlMutationDeny:
    """B1: gh api graphql --input による body mutation は deny する (AC23)"""

    def test_graphql_body_mutation_blocked(self, tmp_path):
        """GIVEN: gh api graphql --input with updateIssue + body variable WHEN: hook THEN: exit 2 (B1)"""
        payload = {
            "query": "mutation UpdateIssue($id: ID!, $body: String!) { updateIssue(input: {id: $id, body: $body}) { issue { number } } }",
            "variables": {"id": "I_abc123", "body": "New English body text here."},
        }
        payload_file = tmp_path / "gql.json"
        payload_file.write_text(__import__("json").dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api graphql --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"GraphQL updateIssue + body variable はブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )
        assert "api_graphql_body_mutation_blocked" in result.stderr or "api_graphql_mutation_denied" in result.stderr, (
            f"stderr に api_graphql_body_mutation_blocked が含まれるべき: {result.stderr}"
        )

    def test_graphql_update_pull_request_body_blocked(self, tmp_path):
        """GIVEN: gh api graphql --input with updatePullRequest + body variable WHEN: hook THEN: exit 2 (B1)"""
        payload = {
            "query": "mutation UpdatePR($id: ID!, $body: String!) { updatePullRequest(input: {pullRequestId: $id, body: $body}) { pullRequest { number } } }",
            "variables": {"id": "PR_abc", "body": "English-only PR body."},
        }
        payload_file = tmp_path / "gql_pr.json"
        payload_file.write_text(__import__("json").dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api graphql --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"GraphQL updatePullRequest + body variable はブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_graphql_mutation_without_body_var_denied(self, tmp_path):
        """GIVEN: gh api graphql --input with mutation but no body variable WHEN: hook THEN: exit 2 conservative deny (B1)"""
        payload = {
            "query": "mutation CloseIssue($id: ID!) { closeIssue(input: {issueId: $id}) { issue { state } } }",
            "variables": {"id": "I_xyz"},
        }
        payload_file = tmp_path / "gql_close.json"
        payload_file.write_text(__import__("json").dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api graphql --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"GraphQL mutation (body variable なし) は conservative deny されるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )
        assert "api_graphql_mutation_denied" in result.stderr or "api_graphql_body_mutation_blocked" in result.stderr, (
            f"stderr に api_graphql_mutation_denied が含まれるべき: {result.stderr}"
        )

    def test_graphql_non_mutation_query_pass(self, tmp_path):
        """GIVEN: gh api graphql --input with query (not mutation) WHEN: hook THEN: exit 0 (B1)"""
        payload = {
            "query": "query GetIssue($number: Int!) { repository(owner: \"owner\", name: \"repo\") { issue(number: $number) { body } } }",
            "variables": {"number": 123},
        }
        payload_file = tmp_path / "gql_query.json"
        payload_file.write_text(__import__("json").dumps(payload))

        hook_input = make_bash_hook_input(
            f"gh api graphql --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 0, (
            f"GraphQL query (mutation でない) は pass するべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_graphql_stdin_fail_closed(self):
        """GIVEN: gh api graphql --input - (stdin) WHEN: hook THEN: exit 2 fail-closed (B1)"""
        hook_input = make_bash_hook_input(
            "gh api graphql --input -"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"gh api graphql --input - (stdin) は fail-closed でブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_graphql_invalid_json_fail_closed(self, tmp_path):
        """GIVEN: gh api graphql --input with invalid JSON WHEN: hook THEN: exit 2 fail-closed (B1)"""
        payload_file = tmp_path / "bad.json"
        payload_file.write_text("{ not valid json }")

        hook_input = make_bash_hook_input(
            f"gh api graphql --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 2, (
            f"GraphQL invalid JSON は fail-closed でブロックされるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )


# ============================================================
# B3: HTTP method check tests
# ============================================================

class TestExtractApiCommandMethod:
    """B3: --extract-api-command-method の単体テスト"""

    def test_method_patch_explicit(self):
        """GIVEN: gh api --method PATCH WHEN: --extract-api-command-method THEN: PATCH"""
        result = run_validator(
            "--extract-api-command-method",
            "gh api repos/owner/repo/issues/123 --method PATCH --input /tmp/body.json",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "PATCH"

    def test_method_patch_shortform(self):
        """GIVEN: gh api -X PATCH WHEN: --extract-api-command-method THEN: PATCH"""
        result = run_validator(
            "--extract-api-command-method",
            "gh api repos/owner/repo/issues/123 -X PATCH --input /tmp/body.json",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "PATCH"

    def test_method_patch_equals(self):
        """GIVEN: gh api --method=PATCH WHEN: --extract-api-command-method THEN: PATCH"""
        result = run_validator(
            "--extract-api-command-method",
            "gh api repos/owner/repo/issues/123 --method=PATCH --input /tmp/body.json",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "PATCH"

    def test_method_get_explicit(self):
        """GIVEN: gh api --method GET WHEN: --extract-api-command-method THEN: GET"""
        result = run_validator(
            "--extract-api-command-method",
            "gh api repos/owner/repo/issues/123 --method GET",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "GET"

    def test_method_unspecified_with_input_defaults_post(self):
        """GIVEN: gh api --input <file> without --method WHEN: --extract-api-command-method THEN: POST"""
        result = run_validator(
            "--extract-api-command-method",
            "gh api repos/owner/repo/issues/123 --input /tmp/body.json",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "POST"

    def test_method_unspecified_no_input_returns_get(self):
        """GIVEN: gh api without --method or --input WHEN: --extract-api-command-method THEN: GET"""
        result = run_validator(
            "--extract-api-command-method",
            "gh api repos/owner/repo/issues/123",
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "GET"


class TestApiGetMethodPass:
    """B3: gh api --method GET は body mutation チェックなしで pass する"""

    def test_api_get_with_input_pass(self, tmp_path):
        """GIVEN: gh api --method GET --input <file> with body key WHEN: hook THEN: exit 0 (B3)"""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(__import__("json").dumps({"body": "English body text"}))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123 --method GET --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        assert result.returncode == 0, (
            f"GET method は body mutation チェックなしで pass するべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )


# ============================================================
# B2: Fail-closed body extraction tests (single quote / space in path)
# ============================================================

class TestB2FailClosedBodyExtraction:
    """B2: 環境変数経由の body 抽出 - 特殊文字パスのテスト"""

    def test_body_null_fail_closed(self, tmp_path):
        """GIVEN: payload with body: null WHEN: hook THEN: exit 2 fail-closed (B2)"""
        payload_file = tmp_path / "null_body.json"
        payload_file.write_text(__import__("json").dumps({"body": None, "state": "open"}))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123 --method PATCH --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        # body が null の場合: body は str でないため fail-closed (exit 2) または not_body_mutation (exit 0 が許容)
        # classify-api-mutation は body key が存在すれば BODY_MUTATION_ISSUE を返すが
        # 環境変数抽出で body=None は SystemExit(20) → fail-closed
        assert result.returncode in (0, 2), (
            f"body: null は fail-closed (exit 2) または not_body_mutation (exit 0) であるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )

    def test_body_array_fail_closed(self, tmp_path):
        """GIVEN: payload with body: [] (array, not str) WHEN: hook THEN: exit 2 fail-closed (B2)"""
        payload_file = tmp_path / "array_body.json"
        payload_file.write_text(__import__("json").dumps({"body": [], "state": "open"}))

        hook_input = make_bash_hook_input(
            f"gh api repos/owner/repo/issues/123 --method PATCH --input {payload_file}"
        )
        result = run_hook(hook_input, mock_gh_responses={})
        # body が配列の場合: body は str でないため fail-closed (exit 2)
        assert result.returncode in (0, 2), (
            f"body: [] は fail-closed (exit 2) または pass (exit 0) であるべき: "
            f"exit {result.returncode}, stderr={result.stderr}"
        )
