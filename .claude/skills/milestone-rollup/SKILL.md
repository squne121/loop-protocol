---
name: milestone-rollup
description: Roll up GitHub Milestone progress, assignment integrity, blocked/ready issues, and next action for LOOP_PROTOCOL.
argument-hint: "[milestone-number]"
disable-model-invocation: true
allowed-tools: Bash Read Grep
---

# milestone-rollup

GitHub Milestone の進捗を rollup し、人間向けに「現在の milestone 状態 / assignment integrity / blocked / ready / next-action」を固定フォーマットで提示する。

対象 Milestone はデフォルト `MILESTONE_NUMBER=1`（M1: Foundation Gate (v0.1.x)）。引数で上書き可能。

**Milestone の close は AI が実行しない。** 下記「Close Readiness Predicate」の全条件が揃った場合のみ「人間が close 判断可能」と通知する。

## SSOT 参照

- `docs/dev/milestone-ops.md` — Milestone 運用規約・API エンドポイント・RACI・Close 条件（fail-closed な close predicate / descendant traversal 要件を含む）

---

## 起動方法

```bash
# read-only rollup（デフォルト: Milestone 1）
.claude/skills/milestone-rollup/scripts/milestone_rollup.sh

# Milestone 番号を指定
.claude/skills/milestone-rollup/scripts/milestone_rollup.sh 2

# rollup + Issue コメント投稿
.claude/skills/milestone-rollup/scripts/milestone_rollup.sh --post <issue_number>

# Milestone 番号指定 + コメント投稿
.claude/skills/milestone-rollup/scripts/milestone_rollup.sh 2 --post <issue_number>
```

実装の詳細（API 呼び出し・jq フィルタ・drift 検出ロジック）は `scripts/milestone_rollup.sh` を参照する。

---

## 出力契約（固定フォーマット）

以下の 6 セクションを Markdown/YAML で出力する。Issue コメントまたは PR 本文に貼り付け可能。

```
milestone_state_summary   — Milestone の基本情報（title / state / open_issues / closed_issues / due_on）
assignment_integrity      — expected_set（#146 readback）vs actual_set（live）の drift 検出・PR 混入カウント
ready_issues              — 着手可能な open issue 一覧（Depends On 全 closed・blocked ラベルなし）
blocked_issues            — ブロックされた open issue 一覧（Depends On open あり / state/blocked ラベル）
next_action               — 優先順位付きアクションリスト（PR 混入 > drift > blocked 解消 > ready 着手 > needs-human）
human_escalations         — 人間判断が必要な事項のリスト（なければ「なし」）
```

加えて `close_readiness` を末尾に付与する（下記「Close Readiness Predicate」参照）。

---

## Close Readiness Predicate（fail-closed）

`close_readiness` セクションは以下の全条件を評価する。`docs/dev/milestone-ops.md` の close predicate と同じ基準を使用する。

`close_judgment_available: true` を出力するには以下の全条件を満たす必要がある:

```yaml
close_readiness_predicate:
  direct_check:
    open_issues: 0               # milestone.open_issues = 0
    pr_mixed_count: 0            # PR 混入なし
    assignment_drift: []         # readback drift なし
  descendant_report:
    schema: MILESTONE_DESCENDANT_ROLLUP_V1
    partial: false               # partial=true は close 不可
    warnings: []                 # warnings 非空は close 不可
    open_blocker_count: 0        # open blocker は close 不可
    scope_conflict_count: 0      # scope conflict は close 不可
```

**重要**: 以下のいずれかが存在する場合は `close_judgment_available: false` を返す:

| 状態 | 備考 |
|---|---|
| `open_issues > 0` | direct item が残存 |
| `pr_mixed_count > 0` | PR 混入（invariant violation） |
| `partial == true` | descendant traversal 不完全 |
| `warnings` 非空 | warnings 未解消 |
| `open_blocker_count > 0` | open blocker 未解消 |
| `scope_conflict_count > 0` | scope conflict 未解消 |

`close_readiness` セクションの出力例:

```yaml
close_readiness:
  open_issues: 0
  pr_mixed_count: 0
  descendant_report_schema: MILESTONE_DESCENDANT_ROLLUP_V1
  partial: false
  warnings_count: 0
  open_blocker_count: 0
  scope_conflict_count: 0
  close_judgment_available: true
  # close_judgment_available: true は人間が close 判断可能な状態を示す
  # AI は Milestone close を実行しない（docs/dev/milestone-ops.md 参照）
```

---

## Guardrails

- **AI は Milestone close を実行しない**。Close Readiness Predicate の全条件が揃った場合のみ「人間が close 判断可能」と `close_readiness` セクションに通知する。
- PR 混入（`pull_request != null`）が検出された場合は `human_escalations` に記録し、close 条件を満たさないことを明示する。
- assignment_drift が検出された場合は `human_escalations` に記録する。
- `partial=true` または `warnings` 非空の場合は `close_judgment_available: false` を返す。
- `open_blocker_count > 0` の場合は `close_judgment_available: false` を返す。
- Milestone state の変更（open / closed）は実行しない。
- `--post` 引数には Issue 番号（数値）のみ渡す。milestone-number も数値のみ受け付ける。

---

## Related

- `docs/dev/milestone-ops.md` — Milestone 運用規約 SSOT（API エンドポイント・RACI・Close 条件・fail-closed な close predicate）
- `docs/dev/github-ops.md` — ラベル運用・認証・Body File Guidance
- `docs/dev/workflow.md` — Issue 駆動開発フロー全体
- `#146` — M-A3 Milestone Assignment Readback（expected set の参照元）

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
