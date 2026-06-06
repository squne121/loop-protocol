"""
test_guard_tmp_draft_passthrough.py

guard-japanese-prose.sh の tmp 下書き passthrough / fail-closed / 英語 prose block テスト (#655)。

AC2: tmp/*.md への Write/Edit/MultiEdit は guard を pass し block されない
AC5: stdin / unreadable / invalid_json / null_body / empty_body は fail-closed
AC6: 英語 prose は exit 2 で block され、permissionDecision ask は返らない
AC7: fake gh を PATH 先頭に置き実 GitHub API に依存しない
AC8: --input + -f body= 共存時は --input 側を body source として使用
AC10: -F body=@- (stdin) は fail-closed (deny_stdin_body_uninspectable)
AC11: POST comment route で -f body= を使う場合 body 実体が検査される
AC12: PATCH comment 更新の既存 #652 挙動が regression なく PASS
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent
HOOK_SCRIPT = HOOKS_DIR / "guard-japanese-prose.sh"
PROJECT_DIR = HOOKS_DIR.parent.parent


def _build_fake_gh(responses):
    lines = ["#!/usr/bin/env bash", "", 'ARGS="$*"', ""]
    for pattern, response in responses.items():
        esc = response.replace("'", "'\"'\"'")
        lines += [
            f'if echo "$ARGS" | grep -q "{pattern}"; then',
            f"  printf '%s' '{esc}'",
            "  exit 0",
            "fi",
            "",
        ]
    lines += ['echo "fake_gh: unmatched: $ARGS" >&2', "exit 1"]
    return "\n".join(lines) + "\n"


def run_hook(hook_input, mock_gh=None):
    env = os.environ.copy()
    env["PROJECT_DIR"] = str(PROJECT_DIR)
    env["GUARD_JAPANESE_PROSE_MODE"] = "enforce"
    with tempfile.TemporaryDirectory() as d:
        if mock_gh is not None:
            fp = os.path.join(d, "gh")
            with open(fp, "w") as f:
                f.write(_build_fake_gh(mock_gh))
            os.chmod(fp, 0o755)
            env["PATH"] = d + ":" + env.get("PATH", "")
        return subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=json.dumps(hook_input),
            capture_output=True, text=True, env=env,
        )


def w(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def e(path, old, new):
    return {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": old, "new_string": new}}


def me(path, edits):
    return {"tool_name": "MultiEdit", "tool_input": {"file_path": path, "edits": edits}}


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


# ============================================================
# AC2: tmp draft not blocked
# ============================================================

def test_tmp_draft_not_blocked():
    """AC2: Write to tmp/draft.md with English prose -> exit 0"""
    r = run_hook(w("tmp/draft.md", "This is English prose that would normally be blocked."))
    assert r.returncode == 0, f"Expected pass, got {r.returncode}\n{r.stderr}"


def test_tmp_draft_not_blocked_edit():
    """AC2: Edit tmp/issue_body.md with English new_string -> exit 0"""
    r = run_hook(e("tmp/issue_body.md", "old", "New English prose content."))
    assert r.returncode == 0, f"Expected pass, got {r.returncode}\n{r.stderr}"


def test_tmp_draft_not_blocked_multi_edit():
    """AC2: MultiEdit tmp/pr_body.md -> exit 0"""
    r = run_hook(me("tmp/pr_body.md", [{"old_string": "old", "new_string": "New English prose."}]))
    assert r.returncode == 0, f"Expected pass, got {r.returncode}\n{r.stderr}"


# ============================================================
# AC5: fail-closed cases
# ============================================================

def test_stdin_body_file_fail_closed():
    """AC5: --body-file - (stdin) -> exit 2"""
    r = run_hook(bash("gh issue edit 123 --body-file -"), {})
    assert r.returncode == 2


def test_field_stdin_fail_closed():
    """AC5/AC10: -F body=@- (stdin) -> exit 2 deny_stdin_body_uninspectable"""
    r = run_hook(bash("gh api repos/owner/repo/issues/123 -X PATCH -F body=@-"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"
    assert "stdin" in r.stderr.lower() or "deny_stdin" in r.stderr


def test_unreadable_file_fail_closed():
    """AC5: -F body=@/nonexistent -> exit 2 deny_unreadable_body_file"""
    r = run_hook(bash("gh api repos/owner/repo/issues/123 -X PATCH -F body=@/no/such/missing.md"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"


def test_invalid_json_fail_closed(tmp_path):
    """AC5: --input invalid JSON -> exit 2"""
    f = tmp_path / "bad.json"
    f.write_text("not json")
    r = run_hook(bash(f"gh api repos/owner/repo/issues/1 --method PATCH --input {f}"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"


def test_null_body_fail_closed(tmp_path):
    """AC5: body:null -> exit 2 deny_null_body_public_mutation"""
    f = tmp_path / "p.json"
    f.write_text('{"body": null}')
    r = run_hook(bash(f"gh api repos/owner/repo/issues/1 --method PATCH --input {f}"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"


def test_empty_body_fail_closed(tmp_path):
    """AC5: body:"" -> exit 2 deny_empty_body_public_mutation"""
    f = tmp_path / "p.json"
    f.write_text('{"body": ""}')
    r = run_hook(bash(f"gh api repos/owner/repo/issues/1 --method PATCH --input {f}"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"


# ============================================================
# AC6: English prose blocked, no ask
# ============================================================

def test_english_prose_field_body_blocked():
    """AC6: -f body=English prose -> exit 2, no ask"""
    r = run_hook(bash("gh api repos/owner/repo/issues/1 -X PATCH -f 'body=This is English prose text without Japanese.'"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"
    assert "ask" not in (r.stdout + r.stderr).lower()


def test_structured_deny_no_ask():
    """AC6: gh issue comment --body English -> exit 2, no ask"""
    r = run_hook(bash("gh issue comment 1 --body 'This is all English prose text.'"), {})
    assert r.returncode == 2
    assert "ask" not in (r.stdout + r.stderr).lower()


# ============================================================
# AC8: --input precedence over -f/-F field
# ============================================================

def test_input_precedence_over_field_japanese(tmp_path):
    """AC8: --input Japanese + -f body=English -> exit 0 (--input wins)"""
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"body": "日本語のテキストです。これは十分な日本語です。"}))
    r = run_hook(
        bash(f"gh api repos/owner/repo/issues/1 --method PATCH --input {f} -f 'body=English text'"),
        {"issue view 1 --json body": "日本語のテキストです。"},
    )
    assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"


def test_input_precedence_over_field_english_blocked(tmp_path):
    """AC8: --input English + -f body=Japanese -> exit 2 (--input wins, English blocked)"""
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"body": "This is all English prose."}))
    r = run_hook(
        bash(f"gh api repos/owner/repo/issues/1 --method PATCH --input {f} -f 'body=日本語'"),
        {"issue view 1 --json body": "既存の日本語です。"},
    )
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"


# ============================================================
# AC11: POST comment route body inspection
# ============================================================

def test_comment_create_post_english_blocked():
    """AC11: POST /issues/1/comments -f body=English -> exit 2"""
    r = run_hook(bash("gh api repos/owner/repo/issues/1/comments -X POST -f 'body=This is English prose.'"), {})
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"


def test_comment_create_post_japanese_pass():
    """AC11: POST /issues/1/comments -f body=Japanese -> exit 0"""
    r = run_hook(bash("gh api repos/owner/repo/issues/1/comments -X POST -f 'body=日本語のコメントです。'"), {})
    assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"


# ============================================================
# AC12: PATCH comment regression
# ============================================================

def test_comment_patch_regression_japanese_pass(tmp_path):
    """AC12: PATCH comment --input Japanese -> exit 0 (regression check)"""
    old = "既存の日本語コメント。"
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"body": old + "\n\n追加の日本語内容。"}))
    r = run_hook(
        bash(f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {f}"),
        {"api repos/owner/repo/issues/comments/123 --jq .body": old},
    )
    assert r.returncode == 0, f"got {r.returncode}\n{r.stderr}"


def test_patch_no_regression_english_blocked(tmp_path):
    """AC12: PATCH comment --input adds English -> exit 2 (regression check)"""
    old = "既存の日本語コメント。"
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"body": old + "\n\nNew English prose here."}))
    r = run_hook(
        bash(f"gh api repos/owner/repo/issues/comments/123 --method PATCH --input {f}"),
        {"api repos/owner/repo/issues/comments/123 --jq .body": old},
    )
    assert r.returncode == 2, f"got {r.returncode}\n{r.stderr}"
