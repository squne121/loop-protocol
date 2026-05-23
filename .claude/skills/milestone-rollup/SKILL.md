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

**Milestone の close は AI が実行しない。** `open_issues == 0` かつ `pr_mixed_count == 0` の条件が揃った場合のみ「人間が close 判断可能」と通知する。

## SSOT 参照

- `docs/dev/milestone-ops.md` — Milestone 運用規約・API エンドポイント・RACI・Close 条件

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

加えて `close_readiness`（`open_issues == 0` かつ `pr_mixed_count == 0` の場合 `close_judgment_available: true`）を末尾に付与する。

---

## Guardrails

- **AI は Milestone close を実行しない**。`open_issues == 0` かつ `pr_mixed_count == 0` の場合のみ「人間が close 判断可能」と `close_readiness` セクションに通知する。
- PR 混入（`pull_request != null`）が検出された場合は `human_escalations` に記録し、close 条件を満たさないことを明示する。
- assignment_drift が検出された場合は `human_escalations` に記録する。
- Milestone state の変更（open / closed）は実行しない。
- `--post` 引数には Issue 番号（数値）のみ渡す。milestone-number も数値のみ受け付ける。

---

## Related

- `docs/dev/milestone-ops.md` — Milestone 運用規約 SSOT（API エンドポイント・RACI・Close 条件）
- `docs/dev/github-ops.md` — ラベル運用・認証・Body File Guidance
- `docs/dev/workflow.md` — Issue 駆動開発フロー全体
- `#146` — M-A3 Milestone Assignment Readback（expected set の参照元）

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
