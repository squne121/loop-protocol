# SubAgent Handoff Contract

skill / orchestrator が SubAgent を呼ぶ際の入出力契約。

## 1. ハンドオフファイル

- 配置: `.claude/plans/<task>-handoff.md`（`.gitignore` 済み）
- 内容: SubAgent が読むだけで仕事を完了できる構造化情報
- 命名規則: `<task-type>-<id>-handoff.md`（例: `issue-42-handoff.md`、`pr-review-99-handoff.md`）

## 2. ハンドオフファイルの必須セクション

```markdown
# <Task title>

## 対象
- Issue / PR 番号 と URL
- 関連リソース（worktree パス、ブランチ名、commit SHA 等）

## 入力データ
- 受け入れ条件 / 変更許可領域 / 既知の制約

## 期待する出力
- 構造化フォーマット名（例: POST_MERGE_CLEANUP_REPORT_V1）
- 必須フィールド

## 制約
- やってはいけないこと（例: コード編集禁止、ファイル削除禁止）

## 失敗時の振る舞い
- どのような状況で fail-close するか
```

## 3. SubAgent 呼び出し

```
Task tool:
  subagent_type: <agent-name>
  prompt: |
    .claude/plans/<task>-handoff.md を Read で読み、その指示に従って作業せよ。
    出力は <ContractName> 形式で返すこと。
```

## 4. SubAgent からの返却

- 構造化フォーマットの YAML / JSON 文字列を本文に含める
- 詳細な試行錯誤ログは含めない（メインセッションの Context Rot 防止）
- 失敗時は `status: failed`、`reason: ...`、`recommended_next_action: ...` を含む構造で返す

## 5. ハンドオフファイルのライフサイクル

- 作成: orchestrator が SubAgent を呼ぶ直前
- 更新: SubAgent が結果を追記してもよい（任意）
- 削除: PR マージ後の cleanup フェーズで削除（`.gitignore` 済みなのでコミットには載らない）

## 関連

- [`follow-up-issue-contract.md`](follow-up-issue-contract.md)
- [`machine-readable-handoff-payload.md`](machine-readable-handoff-payload.md)
- `.agents/rules/subagent-design-policy.md`
