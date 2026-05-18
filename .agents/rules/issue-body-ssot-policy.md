# Rule: issue-body-ssot-policy

## 原則

GitHub Issue 本文を **実装契約の Single Source of Truth (SSOT)** として扱う。

- 受け入れ条件 / 非ゴール / Allowed Paths / Verification Commands は Issue 本文に固定する
- PR 本文・コメント・チャット・ドキュメントは「実行ログ」「補足」であり、契約そのものではない
- 契約変更はコメントではなく **Issue 本文の編集** で行う（変更履歴は GitHub 側で追跡可能）

## 適用

- skill / SubAgent は実装開始時に必ず `gh issue view <番号>` で最新本文を取得する
- 本文と齟齬がある PR 本文・コメント・ドキュメントの記述は Issue 本文に従う
- 「コメントに後で書き足したから」「Discussion に書いたから」は契約として認めない

## 編集時の注意

- 必須セクション（背景 / 目的 / 受け入れ条件 / 非ゴール / テスト観点 / 変更許可領域）の見出しは変更しない
- 編集は body-file 経由（[`github-ops-workflow`](github-ops-workflow.md) §2 参照）
- 大幅な仕様変更は別 Issue 化を検討（[`git-policy`](git-policy.md) §1）

## 関連

- [`issueops-common-guard`](issueops-common-guard.md)
- [`issueops-mode-guard`](issueops-mode-guard.md)
