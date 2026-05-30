# Step 5: LOOP_VERDICT 自動読み取り（Mergeability Handling）

PR コメントに記録された LOOP_VERDICT YAML を読み取る決定論的手順。

## 最新コメント抽出

複数の pr-reviewer 投稿がある場合、**最新の verdict コメントを採用**する:

```bash
PR_NUMBER=<LOOP_STATE.pr_number>

LATEST_VERDICT_BODY=$(gh pr view "$PR_NUMBER" \
  --json reviews,comments \
  --jq '
    [(.reviews // []), (.comments // [])]
    | flatten
    | map(select(.body | contains("## LOOP_VERDICT")))
    | sort_by(.createdAt // .submittedAt)
    | last
    | .body
  ')
```

reviews と comments を時系列で結合してから最新 1 件を取得することで、`gh pr review` 経由（reviews）と `gh issue comment` 経由（comments）の混在に対応する。

## YAML フィールド抽出

```bash
VERDICT=$(echo "$LATEST_VERDICT_BODY" | grep -E "^[[:space:]]*verdict:" | head -n1 | sed -E 's/.*verdict:[[:space:]]*//; s/[[:space:]]*$//')
MERGEABLE=$(echo "$LATEST_VERDICT_BODY" | grep -E "^[[:space:]]*mergeable:" | head -n1 | sed -E 's/.*mergeable:[[:space:]]*//; s/[[:space:]]*$//')
# mergeStateStatus と merge_state_status の両形式に対応（dependency: Issue #56 snake_case 統一完了後は merge_state_status のみに統一する）
MERGE_STATE_STATUS=$(echo "$LATEST_VERDICT_BODY" | \
  grep -E "^[[:space:]]*(mergeStateStatus|merge_state_status):" | \
  head -n1 | sed -E 's/^[[:space:]]*(mergeStateStatus|merge_state_status):[[:space:]]*//; s/[[:space:]]*$//')
REVIEWED_HEAD_SHA=$(echo "$LATEST_VERDICT_BODY" | grep -E "^[[:space:]]*reviewed_head_sha:" | head -n1 | sed -E 's/.*reviewed_head_sha:[[:space:]]*//; s/[[:space:]]*$//')
RECOMMENDATIONS=$(echo "$LATEST_VERDICT_BODY" | grep -E "^[[:space:]]*recommendations:" | head -n1 | sed -E 's/.*recommendations:[[:space:]]*//; s/[[:space:]]*$//')
```

- 各 `head -n1` でコメント本文全体での最初の出現を採用（重複行記載は禁止だが防御として最初を採る）
- 値が空 → LOOP_VERDICT 不正として `human_review_required` で停止
- `RECOMMENDATIONS` の有効値は `[]` または `[update_branch]` のみ。それ以外の値（空文字を除く）は unknown recommendation として `human_escalation` で停止する
- `MERGE_STATE_STATUS` は `mergeStateStatus`（camelCase）と `merge_state_status`（snake_case）の両形式を吸収する（dependency: Issue #56 snake_case 統一）

## reviewed_head_sha 整合確認

`CURRENT_HEAD` として PR の現在の `headRefOid` を取得し、`REVIEWED_HEAD_SHA` と照合する:

```bash
CURRENT_HEAD=$(gh pr view "$PR_NUMBER" --json headRefOid --jq .headRefOid)
```

`REVIEWED_HEAD_SHA` と `CURRENT_HEAD` が一致しない場合（stale LOOP_VERDICT 検出）:

- 取得した LOOP_VERDICT は古い head に対するレビューであるため無効とみなし、以降の判定に使用しない
- `termination_reason` は設定しない（失敗ではなく再評価が必要なケースのため）
- Step 4（pr-review-judge）を再委譲し、現在の head に対する最新の LOOP_VERDICT を取得する
- 新しい LOOP_VERDICT が得られた後、改めて Step 5 の判定を最初から実行する。stale な LOOP_VERDICT で BEHIND 分岐その他の判定を継続してはならない

## 判定結果の orchestrator 反映

| verdict | mergeable | merge_state_status | recommendations | 次アクション |
|---|---|---|---|---|
| `APPROVE` | `MERGEABLE` | `CLEAN` or `UNSTABLE` | 任意 | 終了（approved） |
| `APPROVE` | `MERGEABLE` | `BEHIND` | `[update_branch]` | BEHIND 分岐: `recommendations: [update_branch]` 含む場合 — 下記「BEHIND 分岐 routing」参照（失敗時は Escalation Runbook） |
| `APPROVE` | `MERGEABLE` | `BLOCKED` | 任意 | branch protection 設定待ち。人間判断 |
| `REQUEST_CHANGES` | 任意 | 任意 | 任意 | 次イテレーションへ（blockers を fix_delta に） |
| 任意 | `CONFLICTING` | 任意 | 任意 | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | 任意 | `DIRTY` | 任意 | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | `UNKNOWN` | 任意 | 任意 | 5 秒待機 × 最大 3 回 retry、それでも UNKNOWN なら human_escalation |

## BEHIND 分岐 routing

`APPROVE + MERGEABLE + BEHIND`（`recommendations: [update_branch]` 含む）の場合:

1. `UPDATE_BRANCH_REQUEST_V1` を組み立てる:

   ```yaml
   UPDATE_BRANCH_REQUEST_V1:
     repo: <REPO>
     pr_number: <PR_NUMBER>
     expected_head_sha: <REVIEWED_HEAD_SHA>
     update_method: merge_only
     caller: impl-review-loop.step-5
   ```

2. `implementation-worker` に `UPDATE_BRANCH_REQUEST_V1` を渡して委譲する。
   実行手順（`gh api -i -X PUT`、202 poll loop、422/403 分岐）は `implement-issue` SKILL.md の `## update_branch Contract` セクションを参照。

3. `UPDATE_BRANCH_RESULT_V1` を受け取り、`status` で分岐する:

   | status | 次アクション |
   |---|---|
   | `ok` | stale 判定 → Step 2（test-runner）→ Step 4（pr-review-judge）→ Step 5 再実行 |
   | `stale_verdict` | Step 4（pr-review-judge）re-review → Step 5 再実行 |
   | `forbidden` | `termination_reason: human_escalation` を記録して停止 |
   | `validation_failed` | `termination_reason: human_escalation` を記録して停止 |
   | `timeout` | `termination_reason: human_escalation` を記録して停止 |
   | `human_escalation` | 停止して人間判断を仰ぐ |

4. 更新後に `mergeable=CONFLICTING` または `mergeStateStatus=DIRTY` を検出した場合: `CONFLICTING PR Escalation Runbook` を発動する

## 出力

LOOP_VERDICT の解析結果を LOOP_STATE に反映し、Step 5（feedback-and-termination）の判定マトリクスに従って次アクションを決定する。
