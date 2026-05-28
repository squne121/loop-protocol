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
| `APPROVE` | `MERGEABLE` | `BEHIND` | `[update_branch]` | BEHIND 分岐: `recommendations: [update_branch]` 含む場合 — 下記「BEHIND 分岐 routing」参照（失敗時は Escalation Runbook） |
| `APPROVE` | `MERGEABLE` | `BLOCKED` | 任意 | branch protection 設定待ち。人間判断 |
| `REQUEST_CHANGES` | 任意 | 任意 | 任意 | 次イテレーションへ（blockers を fix_delta に） |
| 任意 | `CONFLICTING` | 任意 | 任意 | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | 任意 | `DIRTY` | 任意 | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | `UNKNOWN` | 任意 | 任意 | 5 秒待機 × 最大 3 回 retry、それでも UNKNOWN なら human_escalation |

## BEHIND 分岐 routing

`APPROVE + MERGEABLE + BEHIND`（`recommendations: [update_branch]` 含む）の場合:

1. `implementation-worker` に `gh pr update-branch` の実行を委譲する（具体的な実行手順は implementation-worker 側）
2. 委譲成功後: 既存の TEST_VERDICT / LOOP_VERDICT は headRefOid が変わるため stale とみなす
3. Step 2（test-runner）→ Step 4（pr-review-judge）を再実行し、新しい reviewed_head_sha の LOOP_VERDICT で Step 5 を再判定する
4. 委譲失敗時: `termination_reason: human_escalation` を記録して停止する
5. 更新後に `mergeable=CONFLICTING` または `mergeStateStatus=DIRTY` を検出した場合: `CONFLICTING PR Escalation Runbook` を発動する

## 出力

LOOP_VERDICT の解析結果を LOOP_STATE に反映し、Step 5（feedback-and-termination）の判定マトリクスに従って次アクションを決定する。
