---
name: post-merge-cleanup-executor
description: PR マージ後の mechanical cleanup executor procedure。git/gh 出力分類・main 整合・worktree/branch 削除（cleanup_exec 経由）・parent issue クローズ条件確認・superseded PR 候補抽出・follow-up 候補収集・POST_MERGE_CLEANUP_REPORT_V1 生成を行う deterministic な 8 ステップ手順。main-thread 向け routing instruction は一切含まない。
---

# Post Merge Cleanup Executor

This file is a derived/non-canonical thin wrapper for the Codex repo-local discovery surface.
Before executing this skill, read the canonical body at `../../../.claude/skills/post-merge-cleanup-executor/SKILL.md`.
Do not treat this wrapper as the workflow procedure body — このファイルは非正本な薄い wrapper であり、mechanical cleanup executor の実際の手順は含まないため、必ず上記 canonical 本文を参照すること。
