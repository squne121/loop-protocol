---
name: implement-issue
description: 承認済みの implementation child issue（`issue-contract-review` で go 判定済み）を、Allowed Paths 内で実装し、Verification Commands で検証し、Draft PR を作成して Issue コメントに結果を返すまでを `1 Issue = 1 PR` で進める手順。「Issue ◯◯ 実装して」「implement issue」「この Issue やって」のトリガーで使う。
---

# Implement Issue

Codex custom agent 用の repo-shared skill entrypoint。
この surface を読んだら、canonical body である `../../../.claude/skills/implement-issue/SKILL.md` を続けて全文読む。
`.agents/skills/` は Codex custom agent の repo-local skill surface、`.claude/skills/` は Claude 側 prompt surface として扱う。
