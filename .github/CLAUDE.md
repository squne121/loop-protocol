# .github

## 編集ガード

- `.github/workflows/*` の編集は CI 動作に直結するため、変更時は PR レビューで人間承認を必須とする
- secrets 利用方針・外部 action の pinning（commit SHA 指定）・`pull_request_target` 利用方針の変更は別 Issue として切り出す
