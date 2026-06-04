"""Smoke tests for gh api comment PATCH body mutation guarding."""

import json

from test_guard_api_input import make_bash_hook_input, run_hook


def test_api_input_issue_comment_english_prose_blocks_without_ask_or_structured_deny(tmp_path):
    old_body = "既存の日本語コメントです。"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps({"body": old_body + "\n\nThis is a new English prose comment."})
    )

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {payload_file}"
    )
    result = run_hook(
        hook_input,
        {"api repos/owner/repo/issues/comments/123 --jq .body": old_body},
    )

    assert result.returncode == 2
    assert "ask" not in result.stderr.lower()


def test_api_input_pr_review_comment_english_prose_blocked(tmp_path):
    old_body = "既存の日本語レビューコメントです。"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps({"body": old_body + "\n\nThis is a new English prose review comment."})
    )

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/pulls/comments/456 --method PATCH --input {payload_file}"
    )
    result = run_hook(
        hook_input,
        {"api repos/owner/repo/pulls/comments/456 --jq .body": old_body},
    )

    assert result.returncode == 2


def test_api_input_issue_comment_code_fence_only_pass(tmp_path):
    old_body = "日本語の説明です。\n\n```bash\necho old\n```"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps({"body": "日本語の説明です。\n\n```bash\necho new\n```"})
    )

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {payload_file}"
    )
    result = run_hook(
        hook_input,
        {"api repos/owner/repo/issues/comments/123 --jq .body": old_body},
    )

    assert result.returncode == 0


def test_api_input_issue_comment_identifier_only_pass(tmp_path):
    old_body = "既存の日本語コメントです。"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps({"body": old_body + "\n\n`guard-japanese-prose.sh` `BODY_MUTATION_ISSUE_COMMENT`"})
    )

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {payload_file}"
    )
    result = run_hook(
        hook_input,
        {"api repos/owner/repo/issues/comments/123 --jq .body": old_body},
    )

    assert result.returncode == 0


def test_api_input_issue_comment_url_only_pass(tmp_path):
    old_body = "既存の日本語コメントです。"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps({"body": old_body + "\n\nhttps://example.com/comment-patch-doc"})
    )

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {payload_file}"
    )
    result = run_hook(
        hook_input,
        {"api repos/owner/repo/issues/comments/123 --jq .body": old_body},
    )

    assert result.returncode == 0


def test_api_input_issue_comment_enum_only_pass(tmp_path):
    old_body = "既存の日本語コメントです。"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(
        json.dumps({"body": old_body + "\n\npermissionDecision: deny"})
    )

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {payload_file}"
    )
    result = run_hook(
        hook_input,
        {"api repos/owner/repo/issues/comments/123 --jq .body": old_body},
    )

    assert result.returncode == 0


def test_api_input_comment_payload_parse_failure_blocked(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text("{ invalid json }")

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {payload_file}"
    )
    result = run_hook(hook_input, {})

    assert result.returncode == 2
    assert "api_payload_parse_failed" in result.stderr


def test_api_input_comment_endpoint_parse_failure_blocked(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"body": "English prose only."}))

    hook_input = make_bash_hook_input(
        f"gh api --method PATCH --input {payload_file}"
    )
    result = run_hook(hook_input, {})

    assert result.returncode == 2
    assert "api_payload_parse_failed" in result.stderr


def test_api_input_issue_comment_old_body_fetch_failure_blocked(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"body": "English prose only."}))

    hook_input = make_bash_hook_input(
        f"gh api repos/owner/repo/issues/comments/999 --method PATCH --input {payload_file}"
    )
    result = run_hook(hook_input, {})

    assert result.returncode == 2
    assert "comment_old_body_fetch_failed" in result.stderr
