---
# このファイルは milestone_rollup.sh の出力サンプル（フォーマット確認用）
# 実際の値は rollup 実行時の live データで異なる
# generated_by: milestone-rollup skill (Issue #147)
---

## Milestone Rollup: M1: Foundation Gate (v0.1.x) (#1)

実行時刻: 2026-05-22T07:00:00Z

### milestone_state_summary

```yaml
milestone_number: 1
title: "M1: Foundation Gate (v0.1.x)"
state: open
open_issues: 2
closed_issues: 1
due_on: null
html_url: "https://github.com/squne121/loop-protocol/milestone/1"
```

### assignment_integrity

```yaml
# expected_set: #146 M-A3 Milestone Assignment Readback コメントより
expected_set: [133, 131, 40]
actual_set: [131, 133, 40]
pr_mixed_count: 0
assignment_drift:
  in_expected_not_actual: []
  in_actual_not_expected: []
drift_status: clean
```

### ready_issues

| # | title | reason |
|---|---|---|
| #131 | 実装・検証・レビュー・PR本文更新をAIエージェントに自律ループさせる際の停止条件が弱かった | Depends On なし・blocked ラベルなし |

### blocked_issues

| # | title | reason |
|---|---|---|
| #133 | 導入: GitHub Milestone 運用の実体化 (parent) | Depends On: #148 (open) |

### next_action

1. parent close 阻害: blocked issue #133 の blocker を解消する（#148 をクローズする）
2. 着手可能: #131 を実装キューに追加する

### human_escalations

なし

### close_readiness

```yaml
open_issues: 2
pr_mixed_count: 0
close_judgment_available: false
# close_judgment_available: true は人間が close 判断可能な状態を示す
# AI は Milestone close を実行しない（docs/dev/milestone-ops.md 参照）
```
