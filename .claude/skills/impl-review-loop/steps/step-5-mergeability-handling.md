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

| verdict | mergeable | merge_state_status | 次アクション |
|---|---|---|---|
| `APPROVE` | `MERGEABLE` | `CLEAN` or `UNSTABLE` | 終了（approved） |
| `APPROVE` | `MERGEABLE` | `BEHIND` | BEHIND 分岐: `gh pr update-branch` を実行し、完了後 merge_state_status を再確認する |
| `APPROVE` | `MERGEABLE` | `BLOCKED` | branch protection 設定待ち。人間判断 |
| `REQUEST_CHANGES` | 任意 | 任意 | 次イテレーションへ（blockers を fix_delta に） |
| 任意 | `CONFLICTING` | `DIRTY` | CONFLICTING PR Escalation Runbook 発動 |
| 任意 | `UNKNOWN` | 任意 | 5 秒待機 × 最大 3 回 retry、それでも UNKNOWN なら human_escalation |

## BEHIND 分岐: `gh pr update-branch` 実行手順

`verdict=APPROVE` かつ `mergeable=MERGEABLE` かつ `mergeStateStatus=BEHIND` の場合、以下の手順で base branch に追従させる。

```bash
# 1. update-branch を実行（base の最新コミットを head に merge）
gh pr update-branch "$PR_NUMBER"

# 2. GitHub API が merge_state_status を再計算するまで待機（最大 30 秒）
for i in 1 2 3 4 5 6; do
  sleep 5
  NEW_STATUS=$(gh pr view "$PR_NUMBER" --json mergeStateStatus --jq .mergeStateStatus)
  echo "[update-branch] attempt $i: mergeStateStatus=$NEW_STATUS"
  [ "$NEW_STATUS" != "BEHIND" ] && break
done

# 3. 再確認結果に応じて次アクションを分岐
case "$NEW_STATUS" in
  CLEAN|UNSTABLE)
    echo "[update-branch] OK: mergeStateStatus=$NEW_STATUS → merge 可能"
    ;;
  DIRTY|CONFLICTING)
    echo "[update-branch] CONFLICTING 検出 → CONFLICTING PR Escalation Runbook を発動"
    # BEHIND から update-branch 実行後に CONFLICTING / DIRTY になった場合も
    # 通常の CONFLICTING と同じ Escalation Runbook に従う
    ;;
  BEHIND)
    echo "[update-branch] 依然 BEHIND → human_escalation"
    ;;
  *)
    echo "[update-branch] UNKNOWN または BLOCKED → human_escalation"
    ;;
esac
```

### BEHIND 後 CONFLICTING 発生時の Escalation 経路

`gh pr update-branch` 実行後に `mergeStateStatus` が `DIRTY` または `CONFLICTING` になった場合は、判定表の `任意 | CONFLICTING | DIRTY` 行と同様に **CONFLICTING PR Escalation Runbook** を発動する。

Escalation Runbook 発動時の処理:
1. LOOP_STATE に `conflicting_after_update_branch: true` を記録する
2. orchestrator は `human_review_required: true` を返して人間判断を仰ぐ
3. マージコンフリクトの解消は実装担当者（`implementation-worker`）の責務であり、orchestrator は自動解消を試みない

## 出力

LOOP_VERDICT の解析結果を LOOP_STATE に反映し、Step 5（feedback-and-termination）の判定マトリクスに従って次アクションを決定する。
