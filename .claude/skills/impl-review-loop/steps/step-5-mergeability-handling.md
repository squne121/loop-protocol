# Step 5: LOOP_VERDICT 自動読み取り（Mergeability Handling）

PR コメントに記録された LOOP_VERDICT YAML を読み取る決定論的手順。

## LOOP_VERDICT_V2 フェンス付き YAML の parse 方針

本手順は **`LOOP_VERDICT_V2` の fenced YAML ブロックのみを parse する**。

V2 consumer path では top-level の `mergeStateStatus` / `recommendations` フィールドを参照しない。
これらは V1 互換フィールドであり、V2 では以下のフィールドを使用する:

| V1 top-level（参照しない） | V2 フィールド（使用する） |
|---|---|
| `mergeStateStatus` | `mergeability.merge_state_status` |
| `recommendations` | `required_auto_actions` |
| （なし） | `merge_ready` |

parse 手順:
1. コメント本文全体から **`LOOP_VERDICT_V2:` キーを含む fenced YAML block（` ```yaml ... ``` `）を全て列挙する**。「最初の ```yaml block」に依存してはならない。
2. 複数ブロックが存在する場合は最新 review comment の block を採用する。
3. prose 中（コードブロック外）に `LOOP_VERDICT_V2:` テキストが出現しても無視する。
4. 対象ブロックが抽出できない場合は LOOP_VERDICT 不正として `human_review_required` で停止する。
5. malformed YAML（parse エラー）は `human_escalation` として停止する。
6. top-level の `mergeStateStatus` / `recommendations` フィールド（V1 形式）は V2 consumer path で無視する。
7. V2 ブロック内の各フィールドを読み取る（以下のフィールド抽出セクション参照）。

## 最新コメント抽出

複数の pr-reviewer 投稿がある場合、**最新の verdict コメントを採用**する:

```bash
PR_NUMBER=<LOOP_STATE.pr_number>

LATEST_VERDICT_BODY=$(gh pr view "$PR_NUMBER" \
  --json reviews,comments \
  --jq '
    [(.reviews // []), (.comments // [])]
    | flatten
    | map(select(.body | contains("LOOP_VERDICT_V2")))
    | sort_by(.createdAt // .submittedAt)
    | last
    | .body
  ')
```

reviews と comments を時系列で結合してから最新 1 件を取得することで、`gh pr review` 経由（reviews）と `gh issue comment` 経由（comments）の混在に対応する。

## YAML フィールド抽出（V2、#1869 fix_delta P0-1: strict YAML parser + CLI wrapper に一本化）

**shell grep/sed による YAML 抽出は廃止した。** 以前の版は「最初の ```yaml block」を awk で
抜き出してから `LOOP_VERDICT_V2:` を後方検索していたため、先行する無関係な yaml block が
コメントに含まれると誤動作した。現在は `.claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py`
の CLI wrapper（`extract_latest_loop_verdict_v2()` + `route_loop_verdict_v2()`）が、コメント本文中の
**すべての fenced ```yaml block を列挙**し、`LOOP_VERDICT_V2` キーを含むブロックだけを候補として
採用する。複数マッチした場合は最後（最も新しく追記された）ブロックを採用する。

```bash
gh pr view "$PR_NUMBER" --json reviews,comments \
  --jq '[(.reviews // []), (.comments // [])] | flatten | map(select(.body | contains("LOOP_VERDICT_V2"))) | sort_by(.createdAt // .submittedAt) | last | .body' \
  > /tmp/loop_verdict_comment_body.txt

uv run python3 .claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py \
  --body-file /tmp/loop_verdict_comment_body.txt \
  [--test-verdict-file /tmp/test_verdict.json]
```

CLI は単一の JSON オブジェクトを stdout に出力する（`route` / `fail_closed` / `reason_code` /
`selected_action` / `rerun_required` / `errors` / `extraction_error`）。**exit code は常に 0**
（ブロック抽出失敗・schema 不正・fail_closed も含め、すべて `route` フィールドで表現される
data であり、process failure ではない）。呼び出し側は `route` フィールドで分岐する:

- `route == "fail_closed"` かつ `extraction_error` が非 null → LOOP_VERDICT ブロックが
  抽出できなかった、または malformed YAML だった。Step 4（pr-review-judge）を再委譲する。
- `route == "fail_closed"` かつ `extraction_error == null` → schema 不正（`reason_code` 参照）。
  Step 4 を再委譲する。
- `route == "conflict_hard_stop"` → CONFLICTING PR Escalation Runbook を発動する。
- `route == "approved"` / `"continue_loop"` / `"route_to_update_branch"` /
  `"route_to_body_only_action"` → 下記「判定結果の orchestrator 反映」テーブルおよび
  `step-5-feedback-and-termination.md` の routing に従う。

`required_auto_actions` は **array of objects**（`kind`/`executor`/`skill`/`blocking_merge_ready`/
`mechanical`/`expected_head_sha`）として parse する。string-list（`[update_branch]` 等）は
schema 不正であり `route_loop_verdict_v2.py` が `fail_closed` を返す（`step-5-feedback-and-termination.md`
の `required_auto_actions_schema` 参照）。

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

> **C5 vs C6 競合解消**: 旧テーブルでは `APPROVE + MERGEABLE + CLEAN/UNSTABLE` が即 `終了（approved）` に routing されていたが、
> `required_auto_actions` が残る場合は終了しない。以下のテーブルは `required_auto_actions` gate を先行させる。

| verdict | merge_ready | merge_state_status | required_auto_actions | 次アクション |
|---|---|---|---|---|
| `APPROVE` | `true` | `CLEAN` | `[]` | **終了（approved）**: `step-5-feedback-and-termination.md` の全 gate pass |
| `APPROVE` | `true` | `CLEAN` | 空でない | required_auto_actions 処理（`step-5-feedback-and-termination.md` の routing）→ 終了しない |
| `APPROVE` | `false` | `BEHIND` | 任意 | BEHIND 分岐: 下記「BEHIND 分岐 routing」参照（`termination_reason: approved` は立てない） |
| `APPROVE` | `false` | `BLOCKED` | 任意 | required checks / review / branch protection の未充足を意味する（Git conflict ではない）。CI・review が揃うまで warning として記録し `termination_reason: approved` は立てず、次 test-runner/review サイクルで再評価する |
| `APPROVE` | `false` | `UNSTABLE` | 任意 | Git conflict ではない（required でない check の失敗/pending）。warning として記録し、CI 結果を待って次サイクルで再評価する（`termination_reason: approved` は立てない） |
| `REQUEST_CHANGES` | 任意 | 任意 | 任意 | 次イテレーションへ（blockers を fix_delta に） |
| 任意 | 任意 | `merge_state_status == DIRTY` | 任意 | **hard stop**: CONFLICTING PR Escalation Runbook 発動（#1860 Owner Decision の唯一の hard stop の一つ） |
| 任意 | 任意 | `mergeability.mergeable == CONFLICTING`（`merge_state_status` ではない） | 任意 | **hard stop**: CONFLICTING PR Escalation Runbook 発動（#1860 Owner Decision の唯一の hard stop の一つ） |
| 任意 | 任意 | `UNKNOWN` / null | 任意 | 5 秒待機 × 最大 3 回 bounded retry。retry 後も `UNKNOWN`/null の場合は warning として記録し、最終 merge-ready 判定のみ保留する（`human_escalation` はしない。実装・レビューサイクル自体は継続する） |
| 任意 | 任意 | `DRAFT` / `HAS_HOOKS` | 任意 | Git conflict ではない。他フィールドの判定（`verdict`/`merge_ready`/required_auto_actions）に従って通常どおり処理する |

> **`mergeable` と `merge_state_status` の分離（#1869 fix_delta P0-1）**: `mergeable` の有効値は
> `CONFLICTING` / `MERGEABLE` / `UNKNOWN`。`merge_state_status` の有効値は `BEHIND` / `BLOCKED` /
> `CLEAN` / `DIRTY` / `DRAFT` / `HAS_HOOKS` / `UNKNOWN` / `UNSTABLE`。`merge_state_status ==
> CONFLICTING` という値は GitHub の実 enum に存在しないため、production router
> （`route_loop_verdict_v2.py`）はこれを **schema 不正**として扱い、conflict としては扱わない。

> **APPROVE + BEHIND の termination_reason**: `APPROVE + merge_ready == false`（BEHIND 含む）の場合、
> `termination_reason: approved` を設定してはならない。BEHIND 分岐で update_branch が完了し、
> 再レビューで `merge_ready: true` かつ `required_auto_actions == []` になるまで終了しない。

## BEHIND 分岐 routing

`APPROVE + mergeable == MERGEABLE + merge_state_status == BEHIND`
（`required_auto_actions` に `kind: update_branch` の object を含む場合。
V1 互換の `recommendations: [update_branch]` は V2 では参照しない）の場合:

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

4. 更新後に `mergeability.mergeable == CONFLICTING` または `mergeability.merge_state_status == DIRTY` を検出した場合: `CONFLICTING PR Escalation Runbook` を発動する

## 出力

LOOP_VERDICT の解析結果を LOOP_STATE に反映し、Step 5（feedback-and-termination）の判定マトリクスに従って次アクションを決定する。
