# .github — GitHub 設定領域

## 役割

GitHub Actions ワークフロー・Issue / PR テンプレート・コードオーナー等の GitHub プラットフォーム設定。

## ⚠️ 編集ガード

- `.github/workflows/*` の編集は **CI 動作に直結** するため、人間レビュー必須
- AI が触れる場合は必ず PR レビューで人間承認を得る
- secrets 利用・外部 action の pinning（commit SHA 指定）・`pull_request_target` 利用の方針変更は別 Issue

## サブディレクトリ

| パス | 内容 |
|---|---|
| `.github/workflows/` | GitHub Actions（typecheck / lint / test / build 等） |
| `.github/ISSUE_TEMPLATE/` | Issue Forms（YAML）または Markdown テンプレート |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR テンプレート |
| `.github/CODEOWNERS` | コードオーナー定義（あれば） |

## Issue Forms の規約

- Issue 起票時の必須項目（背景・目的・受け入れ条件・非ゴール・テスト観点・変更許可領域）は Issue Forms で構造化
- skill 側で gh CLI 経由のパース判定を行う前提のため、見出し名を skill と整合させる

## 関連

- ルート `CLAUDE.md`
- `docs/dev/workflow.md`
- `.claude/skills/create-issue/SKILL.md`
- `.claude/skills/implement-issue/SKILL.md`
