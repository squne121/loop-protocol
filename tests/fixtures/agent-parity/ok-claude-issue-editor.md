---
name: issue-editor
description: Test issue editor agent（テスト用の issue editor エージェント。パリティ検証専用の fixture であり実運用では使用しない）
model: sonnet
tools:
  - Bash
  - Read
permissionMode: acceptEdits
disallowedTools:
  - Agent
  - Edit
  - Write
skills:
  - edit-issue
---

## 出力契約（ISSUE_AUTHOR_RESULT_COMPACT_V1 / artifact_only: ISSUE_AUTHOR_RESULT_V1）

最終出力スキーマとして `ISSUE_AUTHOR_RESULT_COMPACT_V1` を使用する（Use `ISSUE_AUTHOR_RESULT_COMPACT_V1` as final output schema）。
内部artifactのみ: `ISSUE_AUTHOR_RESULT_V1`（Internal artifact only: `ISSUE_AUTHOR_RESULT_V1`）。

RUNTIME（実行時情報）
- runtime_dependency_status: codex_skill_required（依存ステータス）
- runtime_followup_route: edit-issue（フォローアップ経路）

既知の制限（Known limitation）
- hooks はローカルの安全策である（hooks are local guardrails）。

ネストされた委譲の禁止（Nested delegation prohibition）: Agent は disallowedTools に含まれる（Agent is in disallowedTools）。
