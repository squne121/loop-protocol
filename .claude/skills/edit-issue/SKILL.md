---
name: edit-issue
description: 既存 GitHub Issue 本文更新を transaction helper に集約する手順。reviewer フィードバックや人間判断結果を反映し、controlled executor lane を使って body/comment mutation を 1 transaction として実行する。issue-author SubAgent や main session が「Issue ◯◯ の本文を修正して」「Issue 本文を更新して」「edit issue」などのトリガーで使う。
---

# Issue 編集

既存 Issue body/comment mutation の本番経路を
`.claude/skills/edit-issue/scripts/edit_issue_txn.py` に集約する。
呼び出し側は candidate body と readiness context を用意し、helper が返す
JSON result を次のルーティング判断に使う。本文の書き戻し authority は
`issue_body.update` / `issue_comment.publish` の controlled executor command id
だけに限定する。

## 依存ポリシー

```yaml
dependency_policy:
  required_for_txn_helper: "#1284 / PR #1295"
  required_for_end_to_end_raw_mutation_removal: "#1291 / PR #1298"
```

- `required_for_txn_helper` は transaction helper 自体の前提条件。
- `required_for_end_to_end_raw_mutation_removal` は local main guard 側の allowlist 整理を含む別 dependency。
- 本 skill の success は helper consumer への移行を意味し、repo 全体で raw mutation 経路の排除が完了したことまでは意味しない。

## 入力

- `issue_number`（必須）
- `reviewer_feedback_url` または `reviewer_feedback_text`（任意）
- `readiness_forwarding_payload`（必須）: `READINESS_FORWARDING_PAYLOAD_V1`
- `new_body_file`（必須）: candidate issue body を保存した repo-relative file
- `comment_mode`（任意）: success comment を controlled publish するかどうかの指定
- `title_update`（任意）: v1 では `required: true` を受け取っても no-mutation fail にする

`READINESS_FORWARDING_PAYLOAD_V1.readiness_result.status` は
`status: go | needs_fix | human_judgment | input_or_runtime_error`
だけを受け付ける。`status: go` の場合は pre-author static readiness blocker なし、
`status: needs_fix` の場合は `errors[]` と `readiness_result_ref` を candidate body 修正の正本に使う。
`status: human_judgment | input_or_runtime_error` の場合は fail-closed で helper の mutation 段へ進めない。

## 入力契約

`docs/dev/agent-skill-boundaries.md` の `ISSUE_EDIT_TXN_INPUT_V1` を正本とする。
呼び出し側は以下のような JSON を repo 配下に書き、helper に渡す。

```json
{
  "schema": "ISSUE_EDIT_TXN_INPUT_V1",
  "issue_number": 1287,
  "repo": "squne121/loop-protocol",
  "new_body_file": "tmp/issue_1287_new.md",
  "readiness_forwarding_payload": {
    "readiness_result": {
      "status": "go",
      "body_sha256": "sha256:...",
      "source_checks": ["contract_readiness_check.py --mode static"],
      "errors": [],
      "readiness_result_ref": "artifacts/.../readiness.json"
    }
  },
  "comment_mode": {
    "mode": "skip"
  },
  "expected_previous_body_sha256": "sha256:...",
  "expected_previous_updated_at": "2026-07-03T10:40:51Z",
  "title_update": {
    "required": false,
    "proposed_title": null,
    "reason": null
  }
}
```

## 手順

### 1. candidate body と readiness context を準備する

- `reviewer_feedback_url` / `reviewer_feedback_text` と `readiness_forwarding_payload` を使って candidate body を生成する
- candidate body は repo-relative file に保存する
- `title_update.required == true` が必要なら本文修正ではなく別ルーティングに分岐する
- body authoring rule は [`../create-issue/references/body-authoring.md`](../create-issue/references/body-authoring.md) を参照する

### 2. helper を起動する

```bash
uv run --locked python3 .claude/skills/edit-issue/scripts/edit_issue_txn.py \
  --input-file tmp/issue_<N>_txn_input.json
```

helper は以下の固定順序で進む。

1. current issue を readback する
2. candidate body を load する
3. stale precondition を確認する
4. guard を実行する
5. hygiene autofix を適用する
6. static readiness check を実行する
7. `issue_body.update` 用 input file を生成する
8. controlled body update を実行する
9. final readback を確認する
10. 必要な場合だけ `issue_comment.publish` を実行する
11. bounded result を出力する

### 3. 失敗時ルーティング

- `title_update.required == true` の場合 → `failed_no_mutation`
- stale precondition / guard / readiness が失敗した場合 → `failed_no_mutation`
- body update 後に final readback が失敗した場合 → `failed_after_mutation`
- body update 成功後に comment publish が失敗した場合 → `failed_after_mutation`
- `readiness_forwarding_payload.readiness_result.status` が `human_judgment` または `input_or_runtime_error` → `human_judgment`

### 4. 出力

`docs/dev/agent-skill-boundaries.md` の `ISSUE_EDIT_TXN_RESULT_V1` を返す。
helper stdout は最後の 1 JSON object のみとし、old/new issue body や child stdout/stderr を含めない。

## ガードレール

- existing issue body/comment mutation の本番経路は `edit_issue_txn.py` 経由に限定する
- helper は `issue_body.update` / `issue_comment.publish` 以外の mutation command id を使わない
- `title_update` は v1 scope 外。controlled title executor が無い限り no-mutation fail にする
- executor input は `artifacts/{issue_number}/issue-metadata/{command-id}/` 配下だけに生成する
- helper は `capture_output=True, text=True, shell=False` で子プロセスを起動し、bounded diagnostics だけを result に残す
