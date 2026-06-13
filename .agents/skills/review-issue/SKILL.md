---
name: review-issue
description: GitHub Issue 本文を `check_issue_contract.py` で決定論的にレビューし、`REVIEW_ISSUE_RESULT_V1` を返す script-first skill。VC の動作検証はしない（pr-review-judge / test-runner の責務）。「Issue ◯◯ レビュー」「review issue」のトリガーで使う。
---

# Review Issue

This file is a derived/non-canonical thin wrapper for the Codex repo-local discovery surface.
Before executing this skill, read the canonical body at `../../../.claude/skills/review-issue/SKILL.md`.
Do not treat this wrapper as the workflow procedure body.
