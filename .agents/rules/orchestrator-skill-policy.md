# Rule: orchestrator-skill-policy

オーケストレーター skill（`impl-review-loop` / `issue-refinement-loop` 等）の共通契約。

## 1. 責務

オーケストレーター skill は **自身で実装・判定をしない**。役割は：

- 入力 Issue / PR を受け取る
- SubAgent / 別 skill / スクリプトを **正しい順番** で呼ぶ
- 各ステップの結果を集約し、終了条件を判定する
- ループの上限・タイムアウトを管理する

## 2. 状態の永続化

- ループ状態は **ファイルシステム経由** で持つ（メモリ内に閉じ込めない）
- 推奨配置: `.claude/plans/loop-<id>/state.yml` 等
- 各 SubAgent の出力は構造化フォーマット（YAML / JSON）で受け、orchestrator が parse

## 3. 終了条件の明示

- ループは無条件で回さない。明示的な終了条件を必ず定義する
- 例: 「pr-reviewer が APPROVE かつ adversarial-review の CRITICAL/HIGH が 0 件」
- 上限回数（例: 5 回）を超えたら fail-close で人間判断を仰ぐ

## 4. 並列化の方針

- 独立した SubAgent 呼び出しは並列化可（context isolation を活用）
- ただし状態に依存する SubAgent は逐次実行

## 5. コンテキスト隔離

- オーケストレーター本体のコンテキストには **判定結果と次アクションだけ** を残す
- 詳細ログ・試行錯誤は SubAgent 内に閉じ込め、要約のみを返却させる（Context Rot 防止）

## 6. ループ上限の例（推奨）

| skill | デフォルト上限 |
|---|---|
| impl-review-loop | 5 回（実装→検証→レビュー→敵対的レビュー サイクル） |
| issue-refinement-loop | 3 回（調査→レビュー→敵対的レビュー→ライター） |

## 関連

- [`subagent-design-policy`](subagent-design-policy.md)
- [`skill-rule-boundary`](skill-rule-boundary.md)
