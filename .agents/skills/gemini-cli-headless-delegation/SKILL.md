---
name: gemini-cli-headless-delegation
description: Gemini CLI を wrapper 経由で非対話 delegation する shared skill。巨大ログ調査、構造化された技術調査、根拠付き比較を構造化 request 契約で委譲したいときに使う。
---

# Gemini CLI Headless Delegation

Codex custom agent 用の repo-shared skill entrypoint。
この surface は discovery 用の thin bridge であり、runtime instruction body の正本は暫定的に `../../../.claude/skills/gemini-cli-headless-delegation/SKILL.md` に残る。
`.agents/skills/` は Codex custom agent の repo-local discovery surface、`.claude/skills/` は Claude 側 prompt surface 兼 canonical body の保管場所として扱う。
