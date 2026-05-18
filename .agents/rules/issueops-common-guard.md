# Rule: issueops-common-guard

Issue / PR への書き込み時の共通ガード。

## 1. 必須テンプレートの使用

新規 Issue は `.github/ISSUE_TEMPLATE/` で定義された Issue Forms（YAML）を使う。
- `implementation` 種別: 実装作業の依頼
- `research` 種別: 調査タスク
- `human-confirm` 種別: 人間判断が必要な保留事項

`gh issue create --template <name>` で明示的に選ぶ。テンプレートを使わない自由形式 Issue は AI からは起票しない（必須項目欠落で skill が動かなくなるため）。

## 2. 書き込み前の preflight

- `gh issue view <番号>` / `gh pr view <番号>` で現状を確認
- 既に同等の更新がされていないか（idempotency）
- 自身（AI）が直前に同じ操作をしていないか（重複コメント防止）

## 3. ラベル整合

- ラベルの追加・削除は [`issue-uncertainty-policy`](issue-uncertainty-policy.md) / [`issueops-mode-guard`](issueops-mode-guard.md) に従う
- 不明なラベルを勝手に新規作成しない（事前に人間に確認）

## 4. アサイン

- AI から自身を assignee に設定しない
- アサイン操作が必要なら Issue コメントで人間に依頼

## 5. close の判定

- Issue クローズは PR マージのトリガで GitHub 自動処理（`Closes #<番号>`）に任せる
- AI から `gh issue close` は実行しない（特例があれば skill 側で明示）

## 関連

- [`github-ops-workflow`](github-ops-workflow.md)
- [`issue-body-ssot-policy`](issue-body-ssot-policy.md)
