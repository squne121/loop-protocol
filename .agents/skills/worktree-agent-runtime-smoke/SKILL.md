---
name: worktree-agent-runtime-smoke
description: linked worktree 内で Claude Code / Codex CLI の fresh runtime を起動し、structured output（既定・常に direct subprocess）または herdr interactive lane（TUI 固有挙動が必要な場合のみ・常に人間の Herdr session とは分離した isolated named session）で観測し、allowlist-only summary evidence を worktree-local に保存する共有 Skill。「runtime smoke」「動作検証を実行」「Claude/Codex を worktree で起動して確認」のトリガーで使う。
---

# Worktree Agent Runtime Smoke

This file is a derived/non-canonical thin wrapper for the Codex repo-local discovery surface.
Before executing this skill, read the canonical body at `../../../.claude/skills/worktree-agent-runtime-smoke/SKILL.md`.
Do not treat this wrapper as the workflow procedure body.
このファイルは Codex 向けの非正本な薄い wrapper である。実行前に必ず上記の canonical な SKILL.md 本文を参照し、この wrapper 自体を手順書として扱わないこと。
