"""Issue #2811 AC5: update_pr.py's pre-write validator gate blocks a malformed marker body."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import update_pr

ISSUE = 2811

VALID_BODY = """## Summary

- PR body validator の marker 検証（実装計画）

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change
- reason: Python validator と test のみを変更するため

## Schema Consumer Inventory

N/A
reason: schema を変更しないため inventory は不要

## Safety Claim Matrix

N/A
reason: safety-sensitive path に該当しない

## Notes

- Related issue: #2811
- 上記は関連する Issue 番号です
- Closes #2811（対象 Issue）

## 受け入れ条件の達成状況

- [x] AC5: 達成（テスト）

## 検証コマンド結果

```text
$ pnpm typecheck
pass
```

## Allowed Paths 遵守

- 変更ファイル: テストのみ
- Allowed Paths 逸脱: なし
"""


def _malformed_block() -> str:
    zeros = "0" * 64
    return (
        "```yaml\n"
        "IMPLEMENTATION_SCOPE_COVERAGE_V1:\n"
        '  schema_version: "WRONG_SCHEMA"\n'
        f"  issue_number: {ISSUE}\n"
        f'  issue_body_sha256: "sha256:{zeros}"\n'
        f'  normalized_scope_manifest_sha256: "sha256:{zeros}"\n'
        '  pr_head_sha: "not-a-valid-sha"\n'
        "  scope_manifest: {}\n"
        "```\n"
    )


def _run_main(tmp_path, monkeypatch, body: str):
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    edit_calls: list[tuple] = []
    gh_calls: list[tuple] = []
    monkeypatch.setattr(update_pr, "update_pr", lambda *args, **kwargs: edit_calls.append((args, kwargs)) or True)
    monkeypatch.setattr(update_pr, "get_linked_issue_body", lambda *_a, **_k: None)

    def _no_gh(*args, **kwargs):
        gh_calls.append((args, kwargs))
        raise AssertionError("gh must not be invoked")

    monkeypatch.setattr(update_pr, "run_gh", _no_gh)
    exit_code = update_pr.main(
        [
            "--pr-number",
            "1",
            "--body-file",
            str(body_file),
            "--repo",
            "squne121/loop-protocol",
            "--linked-issue",
            str(ISSUE),
            "--changed-paths",
            "docs/dev/foo.md",
        ]
    )
    return exit_code, edit_calls, gh_calls


def test_malformed_marker_blocks_gh_pr_edit(tmp_path, monkeypatch, capsys):
    # Control: the same body without a marker passes the gate and reaches the (stubbed) `gh pr edit`.
    exit_code, edit_calls, _ = _run_main(tmp_path, monkeypatch, VALID_BODY)
    capsys.readouterr()
    assert exit_code == 0
    assert len(edit_calls) == 1

    # A malformed marker body is stopped by the existing pre-write validator gate.
    exit_code, edit_calls, gh_calls = _run_main(tmp_path, monkeypatch, VALID_BODY + "\n" + _malformed_block())
    out = capsys.readouterr().out
    assert exit_code == 1
    assert edit_calls == []
    assert gh_calls == []
    assert "ERROR=E_VALIDATION_FAILED" in out
    assert "LP059" in out
