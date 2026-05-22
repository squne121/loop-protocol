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

## Procedure

### Step 0: 実行準備

```bash
MILESTONE_NUMBER=${1:-1}
REPO="squne121/loop-protocol"
```

### Step 1: Milestone 主要フィールドの取得（AC2）

```bash
gh api "repos/${REPO}/milestones/${MILESTONE_NUMBER}" \
  --jq '{
    number: .number,
    title: .title,
    state: .state,
    open_issues: .open_issues,
    closed_issues: .closed_issues,
    due_on: .due_on,
    html_url: .html_url
  }'
```

### Step 2: 全 item 取得と PR 混入チェック（AC3）

全ページを paginate で取得し、`pull_request != null` の item を PR 混入として分類する。

```bash
# 全 item 取得（Issue + PR 両方が返る）
gh api --paginate \
  "repos/${REPO}/issues?milestone=${MILESTONE_NUMBER}&state=all&per_page=100" \
  --jq '.[] | {number, title, state, is_pr: (.pull_request != null), html_url}'
```

```bash
# PR 混入のみ抽出（0 件が正常）
gh api --paginate \
  "repos/${REPO}/issues?milestone=${MILESTONE_NUMBER}&state=all&per_page=100" \
  --jq '[.[] | select(.pull_request != null) | {number, title, state, html_url}]'
```

PR 混入が存在する場合は `human_escalations` に記録し、Milestone close 条件を満たさないことを明示する。

### Step 3: Assignment Drift の検出（AC4）

#146 の `M-A3 Milestone Assignment Readback` コメントから **expected set** を参照し、
live な actual set との差分を `assignment_drift` として表示する。

**Expected set（#146 readback 記録より）**:
- #133: parent issue — M1: Foundation Gate 親トラッカー
- #131: parent issue — impl-review-loop 停止条件整備
- #40: parent issue — Issue contract 基盤整備（closed）

```bash
# Live actual set 取得
gh api --paginate \
  "repos/${REPO}/issues?milestone=${MILESTONE_NUMBER}&state=all&per_page=100" \
  --jq '[.[] | select(.pull_request == null) | .number]'
```

差分判定:
- `in_expected_not_actual`: expected set に存在するが live に存在しない → silent drop の可能性
- `in_actual_not_expected`: live に存在するが expected set に存在しない → 無断追加の可能性

### Step 4: 各 open issue の blocked/ready 分類（AC5）

各 open issue について以下の deterministic ルールで分類する:

| 分類 | 判定条件 |
|---|---|
| `ready` | `issue.state == open` AND `Depends On` が全て closed AND `state/blocked` ラベルなし AND `state/needs-human` ラベルなし |
| `blocked` | `Depends On` に open issue が残っている OR `state/blocked` ラベルあり |
| `needs-human` | `state/needs-human` ラベルあり、または手動判断必要フラグ |
| `unknown` | 上記いずれにも該当しない |

```bash
# 各 open issue のラベルと依存確認
for issue_num in <open_issue_numbers>; do
  gh api "repos/${REPO}/issues/${issue_num}" \
    --jq '{number, title, state, labels: [.labels[].name]}'
done
```

blocked 判定の一次基準は `Depends On` に記載された Issue の close 状態。`state/blocked` ラベルは補助確認のみ（詳細: `docs/dev/github-ops.md`）。

### Step 5: Next-action の優先順位判定（AC6）

以下の優先順位順（高→低）で next-action を 1 件以上提示する:

1. **PR 混入 / silent drop / assignment drift** — 運用不変条件違反として最優先
2. **parent close を阻害する open child issue** — parent tracker の close 阻害
3. **Depends On が全 closed の ready issue** — すぐに着手可能なタスク
4. **needs-human escalation** — 人間判断が必要な事項

### Step 6: 出力フォーマット（AC7）

以下の固定フォーマットで出力する。Issue コメントまたは PR 本文に貼り付け可能な Markdown/YAML。

````markdown
## Milestone Rollup: <milestone_title> (#<number>)

実行時刻: <ISO8601>

### milestone_state_summary

```yaml
milestone_number: <N>
title: "<title>"
state: open | closed
open_issues: <N>
closed_issues: <N>
due_on: <ISO8601 | null>
html_url: "<url>"
```

### assignment_integrity

```yaml
expected_set: [<issue_numbers>]  # #146 M-A3 readback より
actual_set: [<issue_numbers>]    # live readback
pr_mixed_count: <N>
assignment_drift:
  in_expected_not_actual: []
  in_actual_not_expected: []
drift_status: clean | drift_detected
```

### ready_issues

| # | title | reason |
|---|---|---|
| #N | ... | Depends On なし・blocked ラベルなし |

### blocked_issues

| # | title | reason |
|---|---|---|
| #N | ... | Depends On: #M (open) |

### next_action

優先度: <カテゴリ>

1. <action_item>
2. <action_item>

### human_escalations

- <内容>（または「なし」）

### close_readiness

```yaml
open_issues: <N>
pr_mixed_count: <N>
close_judgment_available: true | false
# close_judgment_available: true は人間が close 判断可能な状態を示す
# AI は Milestone close を実行しない
```
````

---

## Read-only Rollup と Comment Posting の分離

本 skill の実行は **read-only rollup**（Step 1〜6）と **comment posting** を分離する。

- **read-only rollup**: 引数なしで実行（データ取得と分析のみ）
- **comment posting**: 明示的な `--post` フラグまたは手動実行のみ

```bash
# read-only rollup のみ（デフォルト）
.claude/skills/milestone-rollup/scripts/milestone_rollup.sh

# rollup + Issue コメント投稿
.claude/skills/milestone-rollup/scripts/milestone_rollup.sh --post <issue_number>
```

---

## Guardrails

- **AI は Milestone close を実行しない**。`open_issues == 0` かつ `pr_mixed_count == 0` の場合のみ「人間が close 判断可能」と `close_readiness` セクションに通知する。
- PR 混入（`pull_request != null`）が検出された場合は `human_escalations` に記録し、close 条件を満たさないことを明示する。
- assignment_drift が検出された場合は `human_escalations` に記録する。
- Milestone state の変更（open / closed）は実行しない。

---

## Related

- `docs/dev/milestone-ops.md` — Milestone 運用規約 SSOT（API エンドポイント・RACI・Close 条件）
- `docs/dev/github-ops.md` — ラベル運用・認証・Body File Guidance
- `docs/dev/workflow.md` — Issue 駆動開発フロー全体
- `#146` — M-A3 Milestone Assignment Readback（expected set の参照元）
