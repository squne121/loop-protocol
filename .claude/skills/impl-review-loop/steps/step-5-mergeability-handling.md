# Step 5: LOOP_VERDICT ルーティング（Mergeability Handling）

Issue #1873 以降、pr-reviewer SubAgent の返す `reviewer_verdict`（`verdict` / `reviewed_head_sha` /
`blockers` / `warnings` の最小 convention）と、control-plane が `gh pr view` で直接取得する
`live_mergeability`（`mergeable` / `merge_state_status`）を突き合わせて次アクションを決定する。
PR コメントとして投稿された YAML を再パースして判定を再構築することはしない
（コメント投稿は監査用の記録であり、production 判定の入力ではない）。

## reviewer_verdict の取得

pr-reviewer SubAgent の Step 4 呼び出し結果から直接受け取る:

```yaml
verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED
reviewed_head_sha: "<pr-reviewer がレビューした時点の PR head SHA>"
blockers:
  - "<具体的な blocker>"
warnings:
  - "<任意>"
```

`verdict` が `_VALID_VERDICTS`（APPROVE / REQUEST_CHANGES / HUMAN_REVIEW_REQUIRED）以外、
または `reviewed_head_sha` が空の場合は `route_loop_verdict_v2()` が `fail_closed` を返す
（`schema_invalid_verdict_value` / `schema_invalid_reviewed_head_sha_empty_or_missing`）。
`merge_ready` / `mergeability` / `required_auto_actions` / `allowed_paths_gate` / `test_verdict` を
`reviewer_verdict` に含めてはならない（含まれていた場合 `schema_invalid_legacy_field_present` で fail-closed）。

## live_mergeability の取得

```bash
LIVE_MERGEABILITY_JSON=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
  --json headRefOid,mergeable,mergeStateStatus)
```

`route_loop_verdict_v2()` に渡す `live_mergeability` は以下の形にマップする:

```yaml
head_sha: <headRefOid>
mergeable: MERGEABLE | CONFLICTING | UNKNOWN
merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN | DRAFT | HAS_HOOKS
```

> **`mergeable` と `merge_state_status` の分離（#1869 fix_delta P0-1）**: `mergeable` の有効値は
> `CONFLICTING` / `MERGEABLE` / `UNKNOWN`。`merge_state_status` の有効値は `BEHIND` / `BLOCKED` /
> `CLEAN` / `DIRTY` / `DRAFT` / `HAS_HOOKS` / `UNKNOWN` / `UNSTABLE`。`merge_state_status ==
> CONFLICTING` という値は GitHub の実 enum に存在しないため、production router
> （`route_loop_verdict_v2.py`）はこれを **schema 不正**として扱い、conflict としては扱わない。

## ルーティング呼び出し

```python
from route_loop_verdict_v2 import route_loop_verdict_v2

decision = route_loop_verdict_v2(
    reviewer_verdict,       # {verdict, reviewed_head_sha, blockers, warnings}
    live_mergeability,      # {head_sha, mergeable, merge_state_status}
)
```

Issue #1870（#1856）: `route_loop_verdict_v2()` は `test_verdict` を一切受け付けない（渡すと
`TypeError`）。BEHIND 判定は `live_mergeability.merge_state_status == "BEHIND"` のみで確定する。
#1856 が確立した protected TEST_VERDICT producer/publisher lane 自体はこの routing とは独立して
存続する（ordinary review routing の入力ではなくなっただけ）。

`decision.route` の値と orchestrator の対応:

| `route` | 意味 | 次アクション |
|---|---|---|
| `approved` | `APPROVE` かつ mergeable/merge_state_status が `MERGEABLE`/`CLEAN` または `MERGEABLE`/`HAS_HOOKS` | 終了（approved）。`step-5-feedback-and-termination.md` の残り gate を確認 |
| `continue_loop` | `REQUEST_CHANGES`（actual conflict がない場合） | 次イテレーションへ（blockers を fix_delta に） |
| `route_stale_head_rereview` | `APPROVE` かつ `reviewed_head_sha != live head_sha`（actual conflict がない場合） | Step 4 を現在の head で再委譲し、Step 5 を最初からやり直す |
| `route_to_update_branch` | `APPROVE` かつ `merge_state_status == BEHIND` | 下記「BEHIND 分岐 routing」参照 |
| `route_human_escalation` | `HUMAN_REVIEW_REQUIRED`（actual conflict がない場合） | 人間判断を仰いで停止（`termination_reason: human_escalation`）。max iteration 到達や secret/protected-path 等の実 hard gate と合わせて、正当な human stop 理由の一つ |
| `conflict_hard_stop` | `mergeable == CONFLICTING` または `merge_state_status == DIRTY`（**verdict に関係なく最優先で評価**） | CONFLICTING PR Escalation Runbook 発動（actual conflict のみ hard stop。#1860 Owner Decision） |
| `fail_closed` | schema 不正、`APPROVE` かつ `blockers` 非空、`mergeability_unknown`、`BLOCKED`/`UNSTABLE`/`DRAFT` | `decision.reason_code` を warning として記録。`mergeability_unknown` は bounded retry（最大 3 回、5 秒間隔）後も `fail_closed` のままなら warning に格下げして継続。`BLOCKED`/`UNSTABLE`/`DRAFT` は current-head required-CI / branch-protection evaluator の判定に委ね、human escalation にはしない |

`UNKNOWN`、`BLOCKED`、`BEHIND`、`UNSTABLE`、`DRAFT`、`HAS_HOOKS` は Git conflict として扱わない
（Safety Invariants）。actual Git conflict として hard stop するのは `mergeable == CONFLICTING` または
`merge_state_status == DIRTY` の場合のみであり、この判定は reviewer verdict（`REQUEST_CHANGES` /
`HUMAN_REVIEW_REQUIRED` を含む）より必ず先に評価される。`DRAFT` は Issue #1873 の Delivery Rule が
Draft PR を要求しているため、単独では human escalation の理由にしない。

## BEHIND 分岐 routing

`decision.route == "route_to_update_branch"` の場合、`decision.selected_action` に
`route_loop_verdict_v2()` が合成した action が入っている（reviewer からは受け取らない）:

```yaml
kind: update_branch
executor: implementation-worker
skill: implement-issue.update_branch
mechanical: true
expected_head_sha: <reviewed_head_sha>
```

1. この action から `UPDATE_BRANCH_REQUEST_V1` を組み立てる:

   ```yaml
   UPDATE_BRANCH_REQUEST_V1:
     repo: <REPO>
     pr_number: <PR_NUMBER>
     expected_head_sha: <selected_action.expected_head_sha>
     update_method: merge_only
     caller: impl-review-loop.step-5
   ```

2. `implementation-worker` に `UPDATE_BRANCH_REQUEST_V1` を渡して委譲する。`implementation-worker` は
   `.claude/skills/implement-issue/scripts/update_branch.py` の canonical invocation を実行する
   worker 実行経路である。REST 呼び出し・202 poll・403 / 422 分岐は `update_branch.py` wrapper 内部の
   責務であり、orchestrator は `UPDATE_BRANCH_RESULT_V1` のみを消費する。詳細は `implement-issue`
   SKILL.md の `## update_branch Contract` セクションを参照。

3. `UPDATE_BRANCH_RESULT_V1` を受け取り、`status` で分岐する:

   | status | 次アクション |
   |---|---|
   | `ok` | Step 2（test-runner）→ Step 4（pr-review-judge）→ Step 5 再実行 |
   | `stale_verdict` | Step 4（pr-review-judge）re-review → Step 5 再実行 |
   | `forbidden` | `termination_reason: human_escalation` を記録して停止 |
   | `validation_failed` | `termination_reason: human_escalation` を記録して停止 |
   | `timeout` | `termination_reason: human_escalation` を記録して停止 |
   | `human_escalation` | 停止して人間判断を仰ぐ |

4. 更新後に `mergeable=CONFLICTING` または `merge_state_status=DIRTY` を検出した場合: `CONFLICTING PR Escalation Runbook` を発動する

## 出力

`route_loop_verdict_v2()` の `RouteDecision` を LOOP_STATE の代わりに control-plane が in-memory で保持し、
`step-5-feedback-and-termination.md` の判定マトリクスに従って次アクションを決定する。
resume/compaction 後は保存された decision を再利用せず、PR head と reviewer 結果を再取得してから再実行する。
