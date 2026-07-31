# Step 5: 判定 / 終了 / フィードバック循環

Step 2-4 の結果を統合して、ループを次イテレーションに進めるか終了するかを判定する。

## 終了条件マトリクス（Issue #1873、`route_loop_verdict_v2()` の `route` を正本とする）

reviewer_verdict（`verdict`/`reviewed_head_sha`/`blockers`/`warnings`）と live_mergeability
（`gh pr view` で取得した `mergeable`/`merge_state_status`）を `route_loop_verdict_v2()` に渡し、
返る `route` で分岐する。詳細な `route` 一覧と判定条件は `step-5-mergeability-handling.md` を参照。

| `route` | アクション |
|---|---|
| `approved` | `termination_reason: approved` を立て、終了処理へ |
| `route_to_update_branch` | 合成された `update_branch` action を worker に委譲し、検証・PR review を再実行（終了しない） |
| `route_stale_head_rereview` | 現在 head で PR review を再実行（終了しない） |
| `continue_loop` | LOOP_STATE.iteration += 1、Step 1 に戻る（blockers を fix_delta として渡す） |
| `route_human_escalation` | `termination_reason: human_escalation` を立て、即停止（`HUMAN_REVIEW_REQUIRED` verdict、または max iteration 到達・secret/protected-path gate 等の実 hard gate の場合のみ） |
| `conflict_hard_stop` | CONFLICTING PR Escalation Runbook 発動（actual conflict のみ。#1860 Owner Decision の唯一の hard stop） |
| `fail_closed`（`mergeability_unknown` / `merge_state_status_*_not_conflict_defer_to_ci_evaluator`） | warning として記録し、bounded retry または次サイクルでの current-head CI / branch-protection 再評価に委ねる（human escalation にはしない） |
| `fail_closed`（`LOOP_STATE.iteration >= LOOP_STATE.max_iterations`） | `termination_reason: max_iterations` を立て、fail-close で人間判断 |

> **注意**: `verdict: APPROVE` 単独では `termination_reason: approved` に到達しない。
> `route_loop_verdict_v2()` が live mergeability（`CLEAN`/`HAS_HOOKS` かつ `MERGEABLE`）を確認し、
> `blockers == []` である場合にのみ `route: approved` を返す。

## human_review_required の扱い（#1869 fix_delta P0-4）

Step 1-4 の SubAgent が返す `human_review_required: true`（真偽値フィールドとしての自己申告）は、
semantic planning・overlap・contract snapshot・body SHA・artifact 異常に起因するものが大半であり、
それ自体には停止権限がない（#1860 Owner Decision）。以下に再定義する:

- `human_review_required: true` を受け取った場合、`termination_reason: human_escalation` を
  **自動では立てない**。理由・evidence を warning として LOOP_STATE / 終了報告コメントに記録し、
  iteration 余裕があれば Step 1 へ戻って継続する。
- 本ループを停止する human veto は、以下のいずれかを **live Issue または live PR コメント上で
  直接確認できた場合に限定** する:
  - current owner による明示的な停止指示（例: PR/Issue コメントでの `REQUEST_CHANGES` や
    「停止してください」の明示発話）
  - `docs/dev/secret-policy.md` の Decision Gate 未通過（secret 関連）
  - `git conflict` / target PR の GitHub mergeability（`mergeable == CONFLICTING` または
    `merge_state_status == DIRTY`。`step-5-mergeability-handling.md` 参照）
- 上記に該当しない `human_review_required: true`（scope-rollup missing、contract snapshot
  invalid、overlap ambiguous 等）は、ループを止める理由にしない。
- これは pr-reviewer の `verdict` が第一級の `HUMAN_REVIEW_REQUIRED` 値である場合（上記
  終了条件マトリクスの `route_human_escalation`）とは別概念である。`verdict:
  HUMAN_REVIEW_REQUIRED` は reviewer の正式な判定結果であり、`route_loop_verdict_v2()` の
  routing に従って正当に human escalation する。

## update_branch の処理手順（Issue #1873: reviewer 自己申告を廃止、control-plane が合成）

`route_loop_verdict_v2()` が `route: route_to_update_branch` を返した場合、`decision.selected_action`
（`kind: update_branch` / `executor: implementation-worker` / `skill: implement-issue.update_branch` /
`mechanical: true` / `expected_head_sha`）を `implementation-worker` に委譲する。

1. `selected_action` を `implementation-worker` に委譲する
2. worker result の `status` で分岐する:
   - `failed` / `blocked` / `permission_blocked` → `termination_reason: human_escalation` で停止
   - `ok` → verification と PR review の両方を再実行する（`update_branch` は常に head SHA を変える）
3. `reviewed_head_sha` が現在 head と不一致の場合、dispatch 前に PR review を再実行する
4. 再実行後に得られた新しい `reviewer_verdict` / `live_mergeability` で再度 `route_loop_verdict_v2()` を評価する

body-only な自動修正（`update_pr_body_hygiene`、`ensure_closing_keyword` 追加等）が必要な場合も、reviewer は
自己申告せず、具体的な内容を `blockers[]`/`warnings[]` に記載する。
control-plane はその記述を読み、`REQUEST_CHANGES` として `continue_loop` 経路で次イテレーションへ回すか、
機械的に修正可能と判断した場合のみ `implementation-worker` の該当 mode（`ensure_closing_keyword` /
`update_pr_body_hygiene`）に委譲する。これらの body-only 対応は head SHA を変えないため、`update_branch`
と異なり verification の再実行は不要で、PR review のみ再実行する。`blockers` が非空のまま
`verdict == APPROVE` を返した reviewer 結果は `route_loop_verdict_v2()` が `fail_closed`
（`approve_with_blockers_inconsistent`）として扱う。

### worker_status_result_routing（`implementation-worker` 結果の routing）

`implementation-worker` へ `update_branch` action（またはその他の機械的修正）を委譲した結果の `status` は
以下の table で分岐する:

```yaml
worker_status_result_routing:
  worker_status_failed:
    route: human_escalation
  worker_status_blocked:
    route: human_escalation
  worker_status_permission_blocked:
    route: human_escalation
  worker_status_stale_verdict:
    route: rereview
    note: "reviewed_head_sha が変わっており verdict が stale。人間判断を仰がず Step 4（pr-review-judge）を re-review してから Step 5 を再実行する（step-5-mergeability-handling.md と整合。stale は route_loop_verdict_v2() 自身も route_stale_head_rereview として扱い、human_escalation にはしない）"
  worker_status_forbidden:
    route: human_escalation
    note: "403 Forbidden — 権限確認が必要"
  worker_status_validation_failed:
    route: human_escalation
    note: "422 Validation failed（expected_head_sha 不一致等）"
  worker_status_timeout:
    route: human_escalation
    note: "タイムアウト"
  worker_status_ok_rerun_required_true:
    route: "rerun verification and pr_review"
    note: "ok でも rerun_required: true の場合は即終了しない"
```

## branch publish の deterministic retry / safety stop（決定的な再試行と安全停止）

branch publish が hook / approval 境界または remote head drift で止まった場合、`gh pr create` の再試行前に次の read-only preflight を必須とする。これは独立した Git push safety 境界であり、semantic review verdict とは無関係に維持する。

1. `git ls-remote --refs --exit-code origin refs/heads/<branch>` または GitHub Branch API で live remote head を読む
2. local remote-tracking ref を使う場合は、同一 decision cycle 内の fetch 成功を `remote_readback_source: fetch_then_show_ref` として記録する
3. `expected_remote_head`、`current_remote_head`、`local_head`、`verified_head`、`declared_publish_head`、`allowed_paths_gate_status`、`remote_readback_source`、`decision_inputs_complete` を `PUBLISH_LANE_DECISION_V1` で照合する
4. `status: allow_retry` の場合だけ bounded publish command を再試行する
5. 不一致時は `PUBLISH_SAFETY_STOP_REPORT_V1` を残し、manual remote update や force update に暗黙フォールバックしない

strict publish lane を Codex hook 側で再利用する場合、terminal command に env binding を付けない。
repo-approved `codex-hook-adapter.mjs` が hook process と fresh git probe から
`CONTROLLED_PUBLISH_CONTEXT_V1` を生成し、policy CLI へ invocation-scoped JSON として注入する。
canonical existing-branch push は policy の controlled transaction が 1 回だけ実行して readback を確認し、
外側の shell command は deny して二重実行を防ぐ。`env LOOP_PUBLISH_...=... rtk git push ...` は
`context_invalid` として fail-closed に扱う。

```yaml
CONTROLLED_PUBLISH_CONTEXT_V1:
  repository: "squne121/loop-protocol"
  issue_number: "<issue-number>"
  active_branch: "<active-branch>"
  head: "<local-head-sha>"
  remote: "origin"
  allowed_paths_digest: "sha256:<digest>"
```

```yaml
PUBLISH_LANE_DECISION_V1:
  status: allow_retry | safety_stop
  publish_failure_reason:
    boundary_layer: worktree_scope_guard_denied
    reason_code: remote_write_requires_approval | branch_mismatch | stale_remote_head | local_head_mismatch | remote_fast_forward_by_same_scope | remote_head_scope_contamination | non_fast_forward_remote_rewrite | allowed_paths_gate_not_ok | publish_guard_context_missing | publish_guard_context_invalid
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  allowed_command: null | "<bounded publish command>"
  postcondition: "remote branch head == local_head"
```

## live mergeability の routing 分類（Issue #1873: merge_ready 自己申告を廃止）

`merge_ready` は reviewer_verdict の一部として受け取らない。live mergeability
（`gh pr view` の `mergeable` / `merge_state_status`）から `route_loop_verdict_v2()` が
直接分類する。分類の詳細と根拠は `route_loop_verdict_v2.py` の `_APPROVABLE_STATUSES` /
`_DEFER_TO_CI_EVALUATOR_STATUSES` を正本とする:

| merge_state_status | route（`mergeable: MERGEABLE` 前提） | 備考 |
|---|---|---|
| `CLEAN` | `approved` | |
| `HAS_HOOKS` | `approved` | merge hooks があるが merge 可能。conflict ではない |
| `DRAFT` | `fail_closed`（defer to CI evaluator） | Draft PR は人間が ready にする。単独では human escalation にしない（#1860 Owner Decision / #1873 Delivery Rule） |
| `UNSTABLE` | `fail_closed`（defer to CI evaluator） | branch protection テスト失敗の可能性。conflict でも自動 human escalation でもない |
| `BLOCKED` | `fail_closed`（defer to CI evaluator） | branch protection 設定待ち。conflict でも自動 human escalation でもない |
| `BEHIND` | `route_to_update_branch` | `step-5-mergeability-handling.md` の BEHIND 分岐参照 |
| `DIRTY`（または `mergeable: CONFLICTING`） | `conflict_hard_stop` | CONFLICTING PR Escalation Runbook 発動。verdict に関係なく最優先 |
| `UNKNOWN`（`mergeable` または `merge_state_status`） | `fail_closed`（`mergeability_unknown`） | 5 秒待機 × 最大 3 回 retry 後も UNKNOWN なら warning として記録し、最終 merge-ready 判定のみ保留する（`human_escalation` はしない） |

`BLOCKED`/`UNSTABLE`/`DRAFT` の実 blocker（required checks が未充足、branch protection 未達成など）
は current-head required-CI / branch-protection evaluator（`wait_ci_checks.py` / `gh pr checks
--required`）の live evidence で判定する。次の test-runner / review サイクルで再評価し、
`route_loop_verdict_v2()` 自身はこれらを human escalation の理由にしない。

### draft_pr_ready

`IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready` はループの終端ステータス（PR を人間マージ判断のまま
残す）であり、GitHub の Draft PR フラグとは別概念。`merge_state_status: DRAFT` は単独では
human escalation の理由にならない（`route: fail_closed`、defer to CI evaluator）。

## REQUEST_CHANGES 時の fix_delta 構築

LOOP_VERDICT.blockers と TEST_VERDICT の失敗内容から fix_delta を生成し、Step 1 の implementation-worker に渡す:

```yaml
fix_delta:
  iteration: <次の iteration 番号>
  blockers:
    - "<LOOP_VERDICT.blockers から抽出>"
  test_failures:
    - "<TEST_VERDICT.result が FAIL の場合の失敗詳細>"
  pr_review_comment_url: <pr-reviewer が投稿した verdict コメントの URL>
```

## AUTONOMY_POLICY_V1 validator gate（termination_reason: approved 前に必須）

`termination_reason: approved` を立てる前に、`validate_autonomy_policy_result.py` を実行する。
非ゼロ終了（exit 1）の場合は `termination_reason: approved` を禁止し、`human_escalation` として停止する。
この validator は agent の read-only 宣言（Edit/Write/MultiEdit tool 非付与）と終了報告 YAML の
必須フィールド充足を確認する独立した安全境界であり、semantic な review verdict の内容そのものは
検証しない。

```bash
# validate_autonomy_policy_result.py は、ループが生成した実際の終了報告ファイルを受け取る。
# $RESULT_FILE は、終了報告コメント本文を gh issue comment で投稿する前に
# 一時ファイルとして書き出したものを指す（自己生成のダミー入力ではない）。
#
# 終了報告コメント本文の例（$RESULT_FILE に書き込む実際の内容）:
#   ## impl-review-loop: 完了 (2024-01-01T00:00:00Z)
#
#   <!-- IMPL_REVIEW_LOOP_RESULT_V1 -->
#   ```yaml
#   IMPL_REVIEW_LOOP_RESULT_V1:
#     schema_version: 1
#     status: draft_pr_ready
#     termination_reason: approved
#     merge_ready: true
#     pr_url: "https://github.com/..."
#   ```
#
# RESULT_FILE は既にループの終了フローで生成されているファイルへのパスを参照する。

uv run python3 .claude/skills/impl-review-loop/scripts/validate_autonomy_policy_result.py \
  --policy docs/dev/autonomy-policy.md \
  --agent-dir .claude/agents \
  --terminal-output-file "$RESULT_FILE"

VALIDATOR_EXIT=$?

if [ "$VALIDATOR_EXIT" -ne 0 ]; then
  echo "AUTONOMY_POLICY_V1 validation failed (exit $VALIDATOR_EXIT). termination_reason: approved is prohibited."
  echo "termination_reason: human_escalation"
  exit 1
fi
```

validator が exit 0 を返した場合のみ、次の終了処理（approved）に進む。
詳細スキーマ: `docs/dev/autonomy-policy.md` の AUTONOMY_POLICY_VALIDATION_RESULT_V1 マーカースキーマ参照。

`merge_ready`（終了報告 YAML の必須フィールド）は `route_loop_verdict_v2()` への **入力**（reviewer
self-report）としては禁止されているが、終了報告の **出力**フィールドとしては live mergeability から
computed する（`merge_state_status in {CLEAN, HAS_HOOKS}` かつ `mergeable == MERGEABLE` の場合 `true`）。
reviewer からの自己申告値をそのまま転記してはならない。

## 終了処理（approved）

```bash
# LOOP_STATE を最終 YAML として会話履歴に記録
# PR は人間がマージ判断（orchestrator はマージしない）

# Issue コメントで終了報告（機械可読フィールドを含む）
gh issue comment <issue_number> --body "## impl-review-loop: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <最終 iteration 数>
- verdict: APPROVE
- route: approved
- PR: <PR URL>
- 次アクション: 人間レビュー → マージ → post-merge-cleanup

\`\`\`yaml
IMPL_REVIEW_LOOP_RESULT_V1:
  schema_version: 1
  status: draft_pr_ready
  pr_url: \"<PR URL>\"
  head_sha: \"<HEAD SHA>\"
  issue_number: <ISSUE NUMBER>
  termination_reason: approved
  merge_ready: <live merge_state_status in {CLEAN, HAS_HOOKS} and mergeable == MERGEABLE>
  iteration: <最終 iteration 数>

FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: impl-review-loop
  follow_up_issues: # 空の場合も省略しない
    - request_dedupe_key: \"...\"
      status: created | reused_open
      issue:
        number: 123
        url: \"https://github.com/...\"
      reason: null
    - request_dedupe_key: \"...\"
      status: skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
      issue: null
      reason: \"<skipped の理由>\"
  note_only_observations: # 空の場合も省略しない
    - dedupe_key: \"...\"
      source_url: \"...\"
      source_note_id: \"...\"
      summary: \"...\"

# 空の場合の形式（省略禁止）
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: impl-review-loop
  follow_up_issues: []
  note_only_observations: []
\`\`\`"
```

### APPROVE 時の follow-up Issue 自動起票

Issue #1873 以降、follow-up Issue 提案は専用の構造化フィールドでは受け渡さない。pr-reviewer は
follow-up 候補を `reviewer_verdict.warnings[]`（non-blocker の場合）または `blockers[]`（APPROVE を
妨げる場合）のテキストとして記載する。control-plane（main thread）はそのテキストを読み、以下の
優先度で判断する:

- APPROVE を妨げない改善提案として明記されたテキストで、独立した follow-up Issue 化が妥当と main thread が
  判断したもの → APPROVE 確定後に `issue-author` SubAgent に委譲して `create-issue` 経由で起票する
- APPROVE 前に materialize が必須と reviewer が明記したもの（`severity: mandatory_follow_up` 相当の記述）
  → APPROVE 確定**前**に create/reuse する。未 materialize の状態で APPROVE してはならない
- 単なる観察・記録のみでよいもの → 終了報告コメントの本文に記載するのみで起票しない

新規の構造化 schema field（`follow_up_issue_requests[]` 相当）は追加しない（AC13）。

**mandatory_follow_up の処理タイミング**: `severity: mandatory_follow_up` のリクエストは APPROVE 確定**前**に create/reuse する。未 materialize の状態で APPROVE してはならない。

**delivery-rollup parent の残り child 起票（mandatory_follow_up）**:

linked issue の parent が `parent_mode: delivery-rollup` の場合、APPROVE 確定前に以下を実行する:

1. `plan_child_materialization.py` を実行して parent の残り child を確認する（read-only）:
   ```bash
   uv run python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
     --repo <owner>/<repo> \
     --issue <parent_issue_number>
   ```

2. `CHILD_MATERIALIZATION_PLAN_V2.children` に `action: create_issue` のエントリがある場合:
   - 各エントリを `severity: mandatory_follow_up` の候補として main thread が in-memory で保持する follow-up 候補リストに追加する（`FOLLOW_UP_ISSUE_REQUEST_V1` 形式の値は使うが、reviewer_verdict の schema field としては受け渡さない）
   - dedupe_key は `CHILD_MATERIALIZATION_PLAN_V2.children[*].dedupe_key` を使用する

3. `action: reuse_and_update_parent` のエントリがある場合:
   - `edit-issue` skill の `delivery-rollup-parent-update` mode に委譲して parent body の placeholder を修正する

4. `action: human_escalation` のエントリがある場合:
   - `human_review_required: true` で停止し、人間判断を仰ぐ

スキーマ: `CHILD_MATERIALIZATION_PLAN_V2` の正本は `docs/dev/agent-skill-boundaries.md` を参照。

pr-reviewer が `reviewer_verdict.warnings[]`（non-blocker の改善提案・観察事項）に記載したテキストから
main thread が follow-up 候補を抽出する場合も、上記と同じ in-memory リストに追加する
（構造化 field としては受け渡さない。値の形は参考として `docs/dev/agent-skill-boundaries.md` の
`FOLLOW_UP_ISSUE_REQUEST_V1` を流用してよい）。

```
for each req in <main thread が保持する follow-up 候補リスト>:
  - severity: mandatory_follow_up → APPROVE 前に必ず起票（dedupe_key チェック後）
  - severity: optional_follow_up → APPROVE 後に dedupe_key チェック後、重複なければ起票
  - severity: note_only → 起票せず、終了報告コメントの note_only_observations に記録

  dedupe チェック（severity: mandatory_follow_up / optional_follow_up）:
    gh issue list --repo squne121/loop-protocol --state all \
      --search '"<req.dedupe_key>"' --json number,title,url,state,stateReason,labels
    重複あり（open）→ スキップ（既存 Issue 番号を記録、status: reused_open）
    重複あり（closed / not_planned）→ 起票せずスキップ（status: skipped_closed_not_planned）
    重複あり（closed / completed）→ 起票せずスキップ（status: skipped_closed_completed）
    重複あり（closed / duplicate）→ 起票せずスキップ（status: skipped_closed_duplicate）
    重複なし → 起票（## Source セクションに dedupe_key を含める）
    ※ closed Issue を open に差し戻す場合は human escalation が必要（自動起票不可）
```

起票・スキップした follow-up Issue の情報を終了報告コメントの `follow_up_issues` フィールドに列挙する。

follow-up の起票判断そのものは advisory な提案整理であり、`termination_reason: approved` を
ブロックする独立安全境界ではない（mandatory_follow_up の materialize 完了確認を除く）。

## 終了処理（max_iterations）

```bash
gh issue comment <issue_number> --body "## impl-review-loop: max_iterations 到達 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 上限 iteration: <max_iterations>
- 最終 blockers: <LOOP_STATE.blockers_history の最新>
- PR: <PR URL>
- 人間判断を仰ぎます: 追加 iteration を許可するか、別アプローチを検討するか"
```

## 終了処理（human_escalation）

```bash
gh issue comment <issue_number> --body "## impl-review-loop: 人間判断要請 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- 発生 step: <last_step>
- 詳細: <実 hard gate（conflict / secret / max_iteration / 明示的な停止指示）の理由>
- PR: <PR URL>
- 人間の確認後、ループ再開または別アプローチを選択してください"
```

## Publish Failure Safety Lane（publish 失敗時の安全レーン）

implementation-worker / open-pr が branch publish 境界で停止した場合、CI 結果や手動 remote 更新の事後成功だけで安全扱いしてはならない。
以下の順序で readback し、`PUBLISH_LANE_DECISION_V1` または `PUBLISH_SAFETY_STOP_REPORT_V1` を残す。
これも独立した Git push safety 境界であり、semantic review verdict とは無関係に維持する。

1. live readback: remote branch head と local worktree HEAD を読み取る。`ls_remote` / `github_branch_api` / `fetch_then_show_ref` の source を記録する。
2. expected/current/local head comparison: `expected_remote_head`、`current_remote_head`、`local_head`、`verified_head`、`declared_publish_head` を比較する。
3. allowed publish lane decision: `remote == origin`、`active_branch == target_branch`、`expected_remote_head == current_remote_head`、`local_head == declared_publish_head`、`local_head == verified_head`、`allowed_paths_gate_status == ok`、`decision_inputs_complete == true` が全て真の場合だけ `allow_retry` とする。
4. post-publish readback: retry が許された場合も、実行後に remote branch head が `local_head` と一致することを読み戻す。
5. safety stop report: いずれかの比較が崩れた場合は force update / reset を実行せず停止する。

```yaml
PUBLISH_LANE_DECISION_V1:
  status: allow_retry | safety_stop
  publish_failure_reason:
    boundary_layer: worktree_scope_guard_denied | git_remote_rejected | codex_permission_request_no_decision
    reason_code: remote_write_requires_approval | hook_policy_denied | branch_mismatch | stale_remote_head | local_head_mismatch | remote_fast_forward_by_same_scope | remote_head_scope_contamination | non_fast_forward_remote_rewrite | allowed_paths_gate_not_ok | publish_guard_context_missing | publish_guard_context_invalid
  expected_remote_head: "<sha>"
  current_remote_head: "<sha>"
  local_head: "<sha>"
  verified_head: "<sha>"
  declared_publish_head: "<sha>"
  allowed_paths_gate_status: ok | fail_closed | indeterminate
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  allowed_command: null | "<bounded publish command>"
  postcondition: "remote branch head == local_head"
  required_human_decision: []
```

```yaml
PUBLISH_SAFETY_STOP_REPORT_V1:
  status: safety_stop
  redacted_command: "<command>"
  boundary_layer: "<layer>"
  reason_code: "<reason>"
  expected_remote_head: "<sha>"
  current_remote_head: "<sha>"
  local_head: "<sha>"
  verified_head: "<sha>"
  declared_publish_head: "<sha>"
  allowed_paths_gate_status: ok | fail_closed | indeterminate
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  required_decision:
    - "PR branch を linked issue 専用 head へ戻す"
    - "混入 commit を別 PR / 別 branch へ退避する"
```

## ISSUE_SCOPE_ROLLUP_DECISION_V2 の常時記録（advisory）

`ISSUE_SCOPE_ROLLUP_DECISION_V2` は、統合を実施した場合・しなかった場合を問わず、
**ループの全終了経路（approved / max_iterations / human_escalation）で記録を試みる**advisory な
audit trail である。この記録の有無や内容は `termination_reason` の決定に影響しない
（semantic termination authority ではない）。

終了報告コメントに以下を含める:

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<preparation Step 2.5 で生成した plan の body_sha256>"
    generated_at: "<plan の generated_at>"
  decision: executed | skipped | deferred | human_review_required
  executed_actions: []           # 統合を実施した場合のみ設定
  skipped_reason: null           # decision: skipped の場合の理由（例: "no high-confidence candidates"）
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null
```

**記録の原則**:
- preparation Step 2.5 で scope rollup preflight を実行しなかった場合でも `decision: skipped` として記録を試みる。
- `candidates_reviewed` は空配列（`[]`）でも記録する（候補なしの場合）。
- この advisory 記録が欠落・invalid であっても、`termination_reason` の決定（approved / max_iterations /
  human_escalation）をブロックしない。

## Output（出力）

各終了条件に応じた状態を、main thread の一時情報として会話履歴に記録する。永続 artifact としての
replay を必須にしない（resume/compaction 後は live Issue/PR/diff/CI を再取得してから再実行する）:

```yaml
LOOP_STATE:
  target_pr: <PR番号>
  current_head: <現在の head SHA>
  iteration: <最終 iteration 数>
  max_iterations: <上限>
  last_step: judgment
  termination_reason: approved | max_iterations | human_escalation
  last_route: approved | continue_loop | route_to_update_branch | route_stale_head_rereview | route_human_escalation | conflict_hard_stop | fail_closed | null
  unresolved_blockers: []
  scope_rollup_decision: <ISSUE_SCOPE_ROLLUP_DECISION_V2、advisory>
```

その後、orchestrator は次のユーザー入力を待つ（自動で次イテレーションに進む決定済みなら Step 1 を再呼び出し）。
