---
name: pr-review-judge
description: implementation child issue に紐づく PR をレビューするときに使う。linked issue の contract（AC / Allowed Paths / Verification Commands）と PR 本文 / diff / 検証証跡を照合し APPROVE / REQUEST_CHANGES を判定する。self-authored PR は `gh pr review --comment` で verdict を記録する（`--approve` / `--request-changes` は使わない）。LOOP_VERDICT YAML を verdict コメントに含めて impl-review-loop の自動判定に使えるようにする。
---

# PR Review Judge

This file is a derived/non-canonical thin wrapper for the Codex repo-local discovery surface.
Before executing this skill, read the canonical body at `../../../.claude/skills/pr-review-judge/SKILL.md`.
Do not treat this wrapper as the workflow procedure body.
