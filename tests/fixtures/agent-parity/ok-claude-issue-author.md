---
name: issue-author
description: Test author agent
model: sonnet
tools:
  - Bash
  - Read
permissionMode: acceptEdits
disallowedTools:
  - Agent
  - Edit
  - Write
---

## 出力契約（ISSUE_AUTHOR_RESULT_COMPACT_V1 / artifact_only: ISSUE_AUTHOR_RESULT_V1）

Use `ISSUE_AUTHOR_RESULT_COMPACT_V1` as final output schema.
Internal artifact only: `ISSUE_AUTHOR_RESULT_V1`.

RUNTIME
- runtime_dependency_status: codex_skill_required
- runtime_followup_route: create-issue|edit-issue

Known limitation
- hooks are local guardrails.

Nested delegation prohibition: Agent is in disallowedTools.
