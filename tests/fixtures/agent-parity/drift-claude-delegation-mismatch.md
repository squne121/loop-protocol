---
name: issue-reviewer
description: Delegation drift test reviewer
model: haiku
tools:
  - Bash
  - Read
  - Agent
permissionMode: dontAsk
---

## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）

Use `ISSUE_REVIEW_RESULT_COMPACT_V1` as final output schema.

RUNTIME
- runtime_dependency_status: codex_skill_required
- runtime_followup_route: review-issue

Known limitation
- hooks are local guardrails.
