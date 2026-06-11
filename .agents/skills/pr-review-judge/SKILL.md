---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューするときに使う。linked issue の contract（AC / Allowed Paths / Verification Commands）と PR 本文 / diff / 検証証跡を照合し APPROVE / REQUEST_CHANGES を判定する。self-authored PR は `gh pr review --comment` で verdict を記録する（`--approve` / `--request-changes` は使わない）。LOOP_VERDICT YAML を verdict コメントに含めて impl-review-loop の自動判定に使えるようにする。
---

# PR Review Judge

Codex custom agent 用の repo-shared skill entrypoint。
この surface は discovery 用の thin bridge であり、runtime instruction body の正本は暫定的に `../../../.claude/skills/pr-review-judge/SKILL.md` に残る。
`.agents/skills/` は Codex custom agent の repo-local discovery surface、`.claude/skills/` は Claude 側 prompt surface 兼 canonical body の保管場所として扱う。
