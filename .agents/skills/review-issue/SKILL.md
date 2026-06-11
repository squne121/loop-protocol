---
name: review-issue
description: GitHub Issue 本文を `check_issue_contract.py` で決定論的にレビューし、`REVIEW_ISSUE_RESULT_V1` を返す script-first skill。VC の動作検証はしない（pr-review-judge / test-runner の責務）。「Issue ◯◯ レビュー」「review issue」のトリガーで使う。
---

# Review Issue

Codex custom agent 用の repo-shared skill entrypoint。
この surface を読んだら、canonical body である `../../../.claude/skills/review-issue/SKILL.md` を続けて全文読む。
`.agents/skills/` は Codex custom agent の repo-local skill surface、`.claude/skills/` は Claude 側 prompt surface として扱う。
