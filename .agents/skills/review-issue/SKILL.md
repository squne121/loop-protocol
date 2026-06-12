---
name: review-issue
description: GitHub Issue 本文を `check_issue_contract.py` で決定論的にレビューし、`REVIEW_ISSUE_RESULT_V1` を返す script-first skill。VC の動作検証はしない（pr-review-judge / test-runner の責務）。「Issue ◯◯ レビュー」「review issue」のトリガーで使う。
---

# Review Issue

Codex custom agent 用の repo-shared skill entrypoint。
この surface は discovery 用の thin bridge であり、runtime instruction body の正本は暫定的に `../../../.claude/skills/review-issue/SKILL.md` に残る。
`.agents/skills/` は Codex custom agent の repo-local discovery surface、`.claude/skills/` は Claude 側 prompt surface 兼 canonical body の保管場所として扱う。
