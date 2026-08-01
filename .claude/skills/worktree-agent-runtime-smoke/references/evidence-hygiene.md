# Evidence Hygiene Reference

## 保存先

`<worktree>/artifacts/runtime-smoke/<run-id>/` に保存する（worktree-local untracked
directory。schema、receipt、digest、publisher、approval authority ではない）。

```text
summary.md
native-events.jsonl       # structured lane。native output を bounded／redacted
pane-output.txt           # interactive lane。bounded／redacted
agent-detection.json      # herdr agent explain の native response
session-log-metadata.txt  # requested and available の場合のみ
```

## 保存可能な証跡

- runtime 名と version
- tested HEAD
- repo-relative worktree
- branch
- 実行 lane／transport
- process exit code
- herdr pane ID／agent name
- observed lifecycle state
- native event type の件数
- caller 指定の expected marker の有無
- bounded redacted output
- filesystem／Git postcondition
- session-log metadata の取得可否

## 既定で保存しないもの

- raw prompt 全文
- raw transcript 全文
- reasoning
- tool output 全文
- credential
- environment dump
- 認証情報
- HOME を含む絶対パス
- native session log の worktree への複製

## Redaction 規則

- 絶対パス（`/home/*`、`/root/*`、`/Users/*` 等）は `<redacted>` へ置換する
- 40 文字以上の base64-like token は `<redacted>` へ置換する
- native event は 1 ラン最大 400 行、1 行最大 2000 文字へ bound する
- pane output も同じ bound を適用する

## Session-log metadata boundary（#1887 Design Decision 5）

- structured event または herdr output で判定可能な case では session log を必須にしない
- caller が明示的に要求した場合のみ metadata を読み取る（`--inspect-session-log-metadata`）
- session ID または runtime が公開する session reference に束縛する
- allowlist metadata（`type`、`event`、`role`、`subagent`、`label`、`timestamp`、`ts`、`cwd`、
  `session_id`、`sessionId`）だけを抽出する。`reasoning`、raw prompt、tool output 全文は
  allowlist に含まれず抽出しない
- undocumented な log path または record shape を stable schema として扱わない
- log が見つからないことを runtime 成功・失敗へ自動変換しない
- `--require-session-log-metadata` が指定された case だけ、取得不能を exit 77（SKIP）とする
