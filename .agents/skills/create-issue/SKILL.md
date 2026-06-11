---
name: create-issue
description: ユーザーの要求を Terminal AI Agent が再現可能に作業できる GitHub Issue に整形するときに使う。要求分析・Scope 判定・Issue 本文生成・即時起票を行う。blocking stop（Scope 分割採否・Scope Overlap 3 択）以外は人間承認なしで `.claude/skills/create-issue/scripts/create_issue_txn.py` を実行する。「Issue 起票」「Issue 作って」「create issue」などの短文トリガーで使う。
---

# Create Issue

Codex custom agent 用の repo-shared skill entrypoint。
この surface を読んだら、canonical body である `../../../.claude/skills/create-issue/SKILL.md` を続けて全文読む。
`.agents/skills/` は Codex custom agent の repo-local skill surface、`.claude/skills/` は Claude 側 prompt surface として扱う。
