---
name: create-issue
description: ユーザーの要求を Terminal AI Agent が再現可能に作業できる GitHub Issue に整形するときに使う。要求分析・Scope 判定・Issue 本文生成・即時起票を行う。blocking stop（Scope 分割採否・Scope Overlap 3 択）以外は人間承認なしで `.claude/skills/create-issue/scripts/create_issue_txn.py` を実行する。「Issue 起票」「Issue 作って」「create issue」などの短文トリガーで使う。
---

# Create Issue

This file is a derived/non-canonical thin wrapper for the Codex repo-local discovery surface.
Before executing this skill, read the canonical body at `../../../.claude/skills/create-issue/SKILL.md`.
Do not treat this wrapper as the workflow procedure body.
