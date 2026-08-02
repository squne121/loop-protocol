---
name: issue-reviewer
description: advisory な delegation_intent_hint 不一致を検証するためのテスト用レビューア設定
model: haiku
tools:
  - Bash
  - Read
  - Agent
permissionMode: dontAsk
---

## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）

最終的な出力スキーマとして `ISSUE_REVIEW_RESULT_COMPACT_V1` を使用する。

RUNTIME
- runtime_dependency_status: codex_skill_required（Codex 実行環境に依存する）
- runtime_followup_route: review-issue（後続の遷移先は review-issue とする）

既知の制限事項
- hook はローカルのガードレールに過ぎず、セキュリティ境界ではない。
