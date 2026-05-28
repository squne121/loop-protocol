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
MERGE_STATE_STATUS=$(echo "$LATEST_VERDICT_BODY" | grep -E "^[[:space:]]*mergeStateStatus:" | head -n1 | sed -E 's/.*mergeStateStatus:[[:space:]]*//; s/[[:space:]]*$//')
REVIEWED_HEAD_SHA=$(echo "$LATEST_VERDICT_BODY" | grep -E "^[[:space:]]*reviewed_head_sha:" | head -n1 | sed -E 's/.*reviewed_head_sha:[[:space:]]*//; s/[[:space:]]*$//')
```

- 各 `head -n1` でコメント本文全体での最初の出現を採用（重複行記載は禁止だが防御として最初を採る）
- 値が空 → LOOP_VERDICT 不正として `human_review_required` で停止

## reviewed_head_sha 整合確認

```bash
CURRENT_HEAD=$(gh pr view "$PR_NUMBER" --json headRefOid --jq .headRefOid)

if [ "$REVIEWED_HEAD_SHA" != "$CURRENT_HEAD" ]; then
  echo "[STALE] LOOP_VERDICT は古い head ($REVIEWED_HEAD_SHA) に対するレビュー。current: $CURRENT_HEAD"
  # Step 4 を再委譲して最新 head を再レビューさせる
fi
```

## 判定結果の orchestrator 反映

| verdict | mergeable | merge_state_status | recommendations | 次アクション |
|---|---|---|---|---|
| `APPROVE` | `MERGEABLE` | `CLEAN` or `UNSTABLE` | 任意 | 終了（approved） |
| `APPROVE` | `MERGEABLE` | `BEHIND` | `[update_branch]` | BEHIND 分岐: `LOOP_VERDICT.recommendations` に `update_branch` が含まれる場合に `gh pr update-branch` を実行し、その後 Step 2 / Step 4 を再実行して新しい head で LOOP_VERDICT を得てから Step 5 で再判定する |
| `APPROVE` | `MERGEABLE` | `BLOCKED` | 任意 | branch protection 設定待ち。人間判断 |
| `REQUEST_CHANGES` | 任意 | 任意 | 任意 | 次イテレーションへ（blockers を fix_delta に） |
| 任意 | `CONFLICTING` | 任意 | 任意 | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | 任意 | `DIRTY` | 任意 | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | `UNKNOWN` | 任意 | 任意 | 5 秒待機 × 最大 3 回 retry、それでも UNKNOWN なら human_escalation |

## BEHIND 分岐: `LOOP_VERDICT.recommendations` の routing

`LOOP_VERDICT.recommendations` に `update_branch` が含まれている場合（pr-review-judge が `verdict=APPROVE` かつ `mergeable=MERGEABLE` かつ `branch_behind_main: true` を検出したとき emit される）、以下の手順を実行する。

### Step 1: `gh pr update-branch` の実行（fail-closed）

```bash
# update-branch を実行（デフォルト strategy は merge commit。--rebase オプションで rebase に変更可能）
# 注意: デフォルト（--merge）は base branch の最新を merge commit として取り込む。
# strategy 変更が必要な場合は --rebase を指定すること（将来的に strategy 設定を contract で指定可能にする余地を残す）
if ! gh pr update-branch "$PR_NUMBER"; then
  echo "[update-branch] failed → human_escalation"
  # LOOP_STATE に termination_reason: human_escalation を記録して停止
  # orchestrator は human_review_required: true を返して人間判断を仰ぐ
  exit 1
fi

# headRefOid の変化を記録
NEW_HEAD=$(gh pr view "$PR_NUMBER" --json headRefOid --jq .headRefOid)
echo "[update-branch] update succeeded. new headRefOid=$NEW_HEAD"
```

### Step 2: Step 2（test-runner）と Step 4（pr-review-judge）を再実行

`gh pr update-branch` 実行後は headRefOid が変化するため、既存の TEST_VERDICT / LOOP_VERDICT は stale になる。必ず以下の順序で再実行し、新しい head に対する LOOP_VERDICT を得てから Step 5 で再判定すること。

1. **test-runner（Step 2 相当）を再委譲**: 新しい head の SHA に対して mergeable 検知・Verification Commands を再実行し、新しい TEST_VERDICT を PR コメントに投稿させる
2. **pr-review-judge（Step 4 相当）を再委譲**: 新しい TEST_VERDICT を参照して新しい LOOP_VERDICT を PR コメントに投稿させる
3. **Step 5 で再判定**: 新しい LOOP_VERDICT を `LATEST_VERDICT_BODY` として取得し、判定マトリクスに従って次アクションを決定する

> CLEAN なら merge 可能という直接判断は禁止。update-branch 後は必ず Step 2 / Step 4 を経由した LOOP_VERDICT 再取得が必要。

### BEHIND 後 CONFLICTING 発生時の Escalation 経路

`gh pr update-branch` 実行後に `mergeable` が `CONFLICTING` または `mergeStateStatus` が `DIRTY` になった場合は、判定表の `任意 | CONFLICTING | 任意` / `任意 | 任意 | DIRTY` 行と同様に **CONFLICTING PR Escalation Runbook** を発動する。

Escalation Runbook 発動時の処理:
1. LOOP_STATE に `conflicting_after_update_branch: true` を記録する
2. orchestrator は `human_review_required: true` を返して人間判断を仰ぐ
3. マージコンフリクトの解消は実装担当者（`implementation-worker`）の責務であり、orchestrator は自動解消を試みない

> 注意: `mergeStateStatus` に `CONFLICTING` は存在しない（GitHub GraphQL MergeStateStatus enum の有効値は `CLEAN` / `DIRTY` / `UNSTABLE` / `BEHIND` / `BLOCKED` / `UNKNOWN` / `HAS_HOOKS`）。conflict 状態は `mergeable=CONFLICTING`（mergeable enum 側）または `mergeStateStatus=DIRTY` で判定する。

## 出力

LOOP_VERDICT の解析結果を LOOP_STATE に反映し、Step 5（feedback-and-termination）の判定マトリクスに従って次アクションを決定する。
