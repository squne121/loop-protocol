---
name: issue-author
description: "[DEPRECATED] issue-author は issue-creator（新規起票）と issue-editor（既存修正）に分割された（Issue #1734）。本ファイルは薄い deprecated stub であり、いずれの caller からも起動されない。新規起票は issue-creator を、既存修正は issue-editor を使うこと。"
tools:
  - Bash
  - Read
disallowedTools:
  - Agent
  - Edit
  - MultiEdit
  - Write
model: sonnet
permissionMode: acceptEdits
---

## DEPRECATED

この SubAgent は分割済みです（Issue #1734）。create/edit の手順本文は含みません。

- 新規 Issue 起票 → `issue-creator` SubAgent（`create-issue` skill）
- 既存 Issue 修正 → `issue-editor` SubAgent（`edit-issue` skill）

いずれの呼び出し元も本 `issue-author` を起動してはならない。完全な物理削除は Sibling Child #1952（Codex 側分割）のマージ後、`tests/fixtures/codex-agent-config/expected-runtime-contract.json` の更新とあわせて follow-up で行う（本ファイルの存在は同 fixture の `required_agents.issue-author` エントリが要求している）。

## 出力契約（ISSUE_AUTHOR_RESULT_COMPACT_V1）

parity 上の schema 宣言のみを保持する（実行時にこの schema を生成する手順は本ファイルにはない）。実装は `issue-creator` / `issue-editor` を参照すること。
