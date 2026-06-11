---
name: edit-issue
description: 既存 GitHub Issue 本文の更新手順。reviewer フィードバックや人間判断結果を反映して `gh issue edit` で本文を書き戻すまでの一連の手順を提供する。issue-author SubAgent や main session が「Issue ◯◯ の本文を修正して」「Issue 本文を更新して」「edit issue」などのトリガーで使う。`create-issue`（新規起票）に対する **既存 Issue 修正版**で、Template Guard / Outcome Quality Guard / 必須セクション保持を起票と同じ基準で適用する。
---

# Edit Issue

Codex custom agent 用の repo-shared skill entrypoint。
この surface は discovery 用の thin bridge であり、runtime instruction body の正本は暫定的に `../../../.claude/skills/edit-issue/SKILL.md` に残る。
`.agents/skills/` は Codex custom agent の repo-local discovery surface、`.claude/skills/` は Claude 側 prompt surface 兼 canonical body の保管場所として扱う。
