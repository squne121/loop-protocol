---
name: issue-reviewer
description: Drift test reviewer
model: haiku
tools:
  - Bash
  - Read
permissionMode: dontAsk
disallowedTools:
  - Agent
  - Edit
---

## 出力契約（ISSUE_REVIEW_COMPACT_V2）

Use `ISSUE_REVIEW_COMPACT_V2` as final output schema.

RUNTIME
- runtime_dependency_status: codex_skill_required
- runtime_followup_route: review-issue

Known limitation
- hooks are local guardrails.

Nested delegation prohibition: Agent is in disallowedTools.
