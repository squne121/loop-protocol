# Evidence Hygiene Reference

## 保存先

`<worktree>/artifacts/runtime-smoke/<run-id>/` に保存する（worktree-local untracked
directory。schema、receipt、digest、publisher、approval authority ではない）。
`--output-dir` は排他的作成（既存ディレクトリ・symlink は拒否）とする。

```text
summary.md   # 唯一の永続 evidence ファイル。allowlist-only、redacted。
```

raw native event dump（`native-events.jsonl`）、raw pane transcript
（`pane-output.txt`）、`herdr agent explain` の生 JSON（`agent-detection.json`）、
session-log metadata の raw dump（`session-log-metadata.txt`）は保存しない
（PR #1921 human OWNER fix-delta: 先行 PR #1864 で pane transcript にアカウント情報等が
残留した実績があるため、summary.md への allowlist-only 集約へ縮小した）。

## `summary.md` に保存可能な証跡

- runtime 名と version
- 検証対象の tested HEAD
- repo-relative な worktree のパス
- 対象 branch 名
- 実行 lane（`direct` または `herdr_isolated_session`）
- process exit code
- isolated herdr session 名／pane ID／agent name（識別子のみ。transcript は含まない）
- observed lifecycle state（観測された状態）
- 検出された agent kind／confidence（`herdr agent explain` から抽出した 2 フィールドのみ）
- native event の件数・terminal event の有無
- caller 指定の expected marker の有無
- filesystem／Git postcondition（事後条件）の差分一覧
- isolated session cleanup の試行有無・消失確認可否
- session-log metadata の allowlist キー該当件数（値そのものは含まない）

## 既定で保存しないもの

- raw prompt 全文
- raw transcript 全文（native event 本体、pane 出力本体）
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
- `summary.md` に含める process エラーメッセージ（stderr 抜粋）は上記 redaction を必ず通す

## Session-log metadata boundary（session-log metadata の扱いの境界。#1887 Design Decision 5）

- structured event または herdr output で判定可能な case では session log を必須にしない
- caller が明示的に要求した場合のみ metadata を読み取る（`--inspect-session-log-metadata`）
- allowlist metadata キー（`type`、`event`、`role`、`subagent`、`label`、`timestamp`、`ts`）の
  該当有無だけをカウントし、`summary.md` へは件数のみを記録する。値そのもの・`cwd`・
  `session_id` は抽出・保存しない（PR #1921 P1 fix-delta: 過度に許容的な allowlist を縮小）
- `reasoning`、raw prompt、tool output 全文は allowlist に含まれず抽出しない
- undocumented な log path または record shape を stable schema として扱わない
- log が見つからないことを runtime 成功・失敗へ自動変換しない
- `--require-session-log-metadata` が指定された case だけ、取得不能を exit 77（SKIP）とする
