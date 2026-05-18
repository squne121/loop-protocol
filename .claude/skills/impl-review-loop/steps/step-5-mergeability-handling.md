### Step 5: LOOP_VERDICT 自動読み取り（Mergeability Handling）

PR に記録された LOOP_VERDICT コメントを読み取り、ループ終了条件を判定する。

## local-ci/just-check 条件

- `reviewed_head_sha` の一致判定前に、コミットの `local-ci/just-check` 状態が `success` であることを確認する。
- Commit Status API の `local-ci/just-check` が missing / non-success の場合は loop 終了を保留し、再実行条件に戻す。
- Commit Status の記録 `description` に埋め込まれた `head_sha` が現在 PR head SHA と不一致の場合は、レビュー済み head 不一致として扱い `reviewed_head_sha` ガードを再起動する。

**終了条件（以下すべてを満たすことで loop 終了）:**

LOOP_VERDICT コメント内の YAML ブロックで以下をすべて満たす場合、ループを終了する:
- `verdict: APPROVE`
- `blockers: []`（blocker なし）
- `mergeable: MERGEABLE`（conflict なし）
- `mergeStateStatus` が `CLEAN` または `UNSTABLE`（merge 可能な状態）
- `reviewed_head_sha` が**現在の** PR head SHA と完全一致（未レビュー commit が混入していないこと）

`inline review comment` が存在しても、上記 5 項目が揃っていることが Loop 終了の正本条件（canonical）であり、`inline review comment` 自体は補助 evidence のみとして扱う。

**自動読み取り実装例（jq クエリ）:**

判定は 2 段階:
1. **事前チェック**（`LOOP_VERDICT_SATISFIED`）: verdict / blockers / mergeable / mergeStateStatus のみを評価。
2. **最終ガード**（reviewed_head_sha 抽出 + 比較）: 事前チェックを通過した場合のみ、reviewed_head_sha の有無と現在の PR head SHA との一致を検証する。最終ガードを通過しない限りループは終了しない。

```bash
# --- 事前: LOOP_STATE 最新コメントから omission_count を復元 ---
# bash 変数は iteration（プロセス）を跨いで保持されないため、必ず Issue の
# 最新 LOOP_STATE コメントから前 iteration の reviewed_head_sha_omission_count
# を読み込んで初期化すること。読み取れない場合は 0 とみなす。
REVIEWED_HEAD_SHA_OMISSION_COUNT=$(gh issue view <Issue番号> --json comments --jq '
  ([.comments[] | select(.body | contains("## LOOP_STATE"))] | last).body
' | grep -E "^[[:space:]]*reviewed_head_sha_omission_count:" | head -n1 \
  | sed -E 's/^[[:space:]]*reviewed_head_sha_omission_count:[[:space:]]*//; s/[[:space:]]*#.*$//' \
  | tr -d ' \r')
REVIEWED_HEAD_SHA_OMISSION_COUNT="${REVIEWED_HEAD_SHA_OMISSION_COUNT:-0}"
echo "[LOOP] omission_count restored from LOOP_STATE: $REVIEWED_HEAD_SHA_OMISSION_COUNT"

# --- 事前チェック ---
LOOP_VERDICT_SATISFIED=$(gh pr view <PR番号> --json reviews,comments --jq '
  ([.reviews[] | {body, createdAt: .submittedAt}] + [.comments[] | {body, createdAt}])
  | map(select(.body | contains("## LOOP_VERDICT")))
  | sort_by(.createdAt)
  | .[-1].body
' | python3 -c '
import re
import sys

import yaml

body = sys.stdin.read()
match = re.search(r"## LOOP_VERDICT\s*```yaml\s*(.*?)\s*```", body, re.S)
if not match:
    print("false")
    raise SystemExit(0)

try:
    data = yaml.safe_load(match.group(1))
except yaml.YAMLError:
    print("false")
    raise SystemExit(0)

if not isinstance(data, dict):
    print("false")
    raise SystemExit(0)

ok = (
    data.get("verdict") == "APPROVE"
    and data.get("blockers") == []
    and data.get("mergeable") == "MERGEABLE"
    and data.get("mergeStateStatus") in {"CLEAN", "UNSTABLE"}
)
print("true" if ok else "false")
' || echo 'false')

> **注意**: reviews と comments を結合した際は時系列ソートが必要です。投稿順序によっては最新コメントが異なるため、sort_by(.createdAt) で統一的に最後のコメントを抽出すること。

if [ "$LOOP_VERDICT_SATISFIED" = "true" ]; then
  # --- 最終ガード: reviewed_head_sha の有無・一致検証 ---
  # LOOP_VERDICT に記録された reviewed_head_sha と現在の PR head SHA が
  # 一致することを確認する。不一致の場合は別ワークツリー由来の未レビュー
  # commit が混入しているため、ループを終了せず Step 2+3 を再実行する
  # （PR #744 / Issue #707 事故の再発防止）。
  #
  # 注: sed の 2 段目 's/[[:space:]]*#.*$//' は YAML inline comment
  #     の混入を防ぐ。テストケース:
  #       入力: "reviewed_head_sha: abc123  # 前回の SHA"
  #       出力: "abc123"（comment 部分が完全に除去される）
  REVIEWED_SHA_FROM_VERDICT=$(gh pr view <PR番号> --json reviews,comments --jq '
    (([.reviews[] | {body, createdAt: .submittedAt}] + [.comments[] | {body, createdAt}]) | map(select(.body | contains("## LOOP_VERDICT"))) | sort_by(.createdAt) | .[-1]).body
  ' | grep -E "^[[:space:]]*reviewed_head_sha:" | head -n1 \
    | sed -E 's/^[[:space:]]*reviewed_head_sha:[[:space:]]*//; s/[[:space:]]*#.*$//' \
    | tr -d ' \r')
  CURRENT_STATE=$(gh pr view <PR番号> --json mergeable,mergeStateStatus,headRefOid)
  CURRENT_HEAD_SHA=$(echo "$CURRENT_STATE" | jq -r .headRefOid)

  if [ -z "$REVIEWED_SHA_FROM_VERDICT" ]; then
    # === pr-reviewer 記載漏れケース ===
    # 同一 iteration 内で Step 4 のみを再実行（iteration カウントは +1 しない）。
    # 既存 LOOP_VERDICT コメントは上書きせず残し、pr-reviewer に新規 gh pr review --comment で
    # reviewed_head_sha 必須化を伝達して新しい verdict コメントを投稿させる
    # （Step 5 自動読み取りは `last` で抽出するため新規投稿が正本となる）。
    REVIEWED_HEAD_SHA_OMISSION_COUNT=$((REVIEWED_HEAD_SHA_OMISSION_COUNT + 1))
    echo "[LOOP] LOOP_VERDICT に reviewed_head_sha が記載されていません (omission_count=$REVIEWED_HEAD_SHA_OMISSION_COUNT)"

    # LOOP_STATE に記載漏れカウントを記録（次 iteration での状態継続用に必須）。
    # 既存 SKILL.md の LOOP_STATE 投稿例と同じ "--body" double-quoted 形式を使用し、
    # markdown コードフェンス（\`\`\`）は \\\` でエスケープする。
    gh issue comment <Issue番号> --body "## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: reviewed
status: running
pr_url: <PR_URL>
last_verdict: APPROVE_INCOMPLETE
reviewed_head_sha_omission_count: $REVIEWED_HEAD_SHA_OMISSION_COUNT
note: pr-reviewer が reviewed_head_sha を記載漏れ。Step 4 のみ再実行（iteration 据え置き）。
\`\`\`"

    if [ "$REVIEWED_HEAD_SHA_OMISSION_COUNT" -ge 2 ]; then
      # 2 回連続記載漏れ → 人間エスカレーション（自動ループ継続しない）
      gh issue comment <Issue番号> --body "## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: escalation
status: blocked
pr_url: <PR_URL>
last_verdict: APPROVE_INCOMPLETE
reviewed_head_sha_omission_count: $REVIEWED_HEAD_SHA_OMISSION_COUNT
\`\`\`
## Escalation
pr-reviewer が 2 回連続で LOOP_VERDICT に reviewed_head_sha を記載漏れしました。
pr-review-judge SKILL の必須フィールド規定または委譲プロンプトの再点検が必要です。"
      exit 1
    fi
    # → Step 4 を同一 iteration で再実行（reviewed_head_sha 必須化を委譲プロンプトで強調）
  elif [ "$REVIEWED_SHA_FROM_VERDICT" != "$CURRENT_HEAD_SHA" ]; then
    # === 未レビュー commit 検出ケース ===
    # iteration を +1 し、新 head を REVIEWED_HEAD_SHA として再取得して Step 2+3+4 を再実行
    echo "[LOOP] 未レビュー commit を検出しました: reviewed=$REVIEWED_SHA_FROM_VERDICT, current=$CURRENT_HEAD_SHA"
    git log --oneline "$REVIEWED_SHA_FROM_VERDICT".."$CURRENT_HEAD_SHA" 2>/dev/null || true
    REVIEWED_HEAD_SHA_OMISSION_COUNT=0  # 不一致は記載漏れとは別事象なのでカウンタはリセット
  else
    CURRENT_MERGEABLE=$(echo "$CURRENT_STATE" | jq -r .mergeable)
    CURRENT_MERGE_STATE_STATUS=$(echo "$CURRENT_STATE" | jq -r .mergeStateStatus)

    if [ "$CURRENT_MERGEABLE" = "MERGEABLE" ] && \
       { [ "$CURRENT_MERGE_STATE_STATUS" = "CLEAN" ] || [ "$CURRENT_MERGE_STATE_STATUS" = "UNSTABLE" ] || [ "$CURRENT_MERGE_STATE_STATUS" = "HAS_HOOKS" ]; }; then
      echo "[LOOP] LOOP_VERDICT 条件と reviewed_head_sha / live merge state の一致を確認。ループを終了します。"
      REVIEWED_HEAD_SHA_OMISSION_COUNT=0
      # ループ終了処理へ
    else
      echo "[LOOP] live merge state が終了条件を満たしていません: mergeable=$CURRENT_MERGEABLE, mergeStateStatus=$CURRENT_MERGE_STATE_STATUS"
      REVIEWED_HEAD_SHA_OMISSION_COUNT=0
      # Step 2+3 を再実行して最新 state を再評価する
    fi
  fi
else
  echo "[LOOP] LOOP_VERDICT 条件を満たしていません。修正を続行します。"
  # 次の iteration へ
fi
```

**安全策（オプション）:**

LOOP_VERDICT コメント記録時の mergeable / mergeStateStatus / reviewed_head_sha 値が古い可能性があるため、判定後に API で再確認することを推奨する:

```bash
if [ "$LOOP_VERDICT_SATISFIED" = "true" ] && [ -n "$REVIEWED_SHA_FROM_VERDICT" ]; then
  # LOOP_VERDICT は満たしているが、念のため現在値を確認
  CURRENT_STATE=$(gh pr view <PR番号> --json mergeable,mergeStateStatus,headRefOid)
  echo "最終確認: $CURRENT_STATE"

  # reviewed_head_sha 最終確認（API レスポンスの headRefOid と直接比較）
  FINAL_HEAD_SHA=$(echo "$CURRENT_STATE" | jq -r .headRefOid)
  if [ "$REVIEWED_SHA_FROM_VERDICT" = "$FINAL_HEAD_SHA" ]; then
    echo "[LOOP] reviewed_head_sha 最終確認 OK ($REVIEWED_SHA_FROM_VERDICT)。マージに進む。"
  else
    echo "[LOOP] reviewed_head_sha 最終確認で不一致を検出。Step 2+3 再実行へ。"
    # 現在値が MERGEABLE/CLEAN|UNSTABLE かつ headRefOid == reviewed_head_sha でない限りマージしない
  fi
fi
```
