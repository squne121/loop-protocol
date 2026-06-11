---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup

Codex custom agent 用の repo-shared skill entrypoint。
この surface は discovery 用の thin bridge であり、runtime instruction body の正本は暫定的に `../../../.claude/skills/post-merge-cleanup/SKILL.md` に残る。
`.agents/skills/` は Codex custom agent の repo-local discovery surface、`.claude/skills/` は Claude 側 prompt surface 兼 canonical body の保管場所として扱う。
