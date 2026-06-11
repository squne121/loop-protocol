---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup

Codex custom agent 用の repo-shared skill entrypoint。
この surface を読んだら、canonical body である `../../../.claude/skills/post-merge-cleanup/SKILL.md` を続けて全文読む。
`.agents/skills/` は Codex custom agent の repo-local skill surface、`.claude/skills/` は Claude 側 prompt surface として扱う。
