---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューするときに使う。linked issue の contract（AC / Allowed Paths / Verification Commands）と PR 本文 / diff / 検証証跡を照合し APPROVE / REQUEST_CHANGES を判定する。self-authored PR は `gh pr review --comment` で verdict を記録する（`--approve` / `--request-changes` は使わない）。LOOP_VERDICT YAML を verdict コメントに含めて impl-review-loop の自動判定に使えるようにする。
---

# PR Review Judge

Codex custom agent 用の repo-shared skill entrypoint。
この surface を読んだら、canonical body である `../../../.claude/skills/pr-review-judge/SKILL.md` を続けて全文読む。
`.agents/skills/` は Codex custom agent の repo-local skill surface、`.claude/skills/` は Claude 側 prompt surface として扱う。
