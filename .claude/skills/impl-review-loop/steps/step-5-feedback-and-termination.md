# Step 5: 判定 / 終了 / フィードバック循環

Step 2-4 の結果を統合して、ループを次イテレーションに進めるか終了するかを判定する。

## 終了条件マトリクス

| 条件 | アクション |
|---|---|
| `LOOP_VERDICT.verdict: APPROVE` かつ `merge_ready == true` かつ `required_auto_actions == []` | `termination_reason: approved` を立て、終了処理へ |
| `LOOP_VERDICT.verdict: APPROVE` かつ `required_auto_actions` が空でない | `required_auto_action_result_routing` に従って worker 委譲し、処理完了後に PR review を再実行（終了しない） |
| `LOOP_VERDICT.verdict: APPROVE` かつ `merge_ready == false` | `step-5-mergeability-handling.md` の routing に従う（終了しない） |
| `LOOP_STATE.iteration >= LOOP_STATE.max_iterations` | `termination_reason: max_iterations` を立て、fail-close で人間判断 |
| Step 1-2-4 のいずれかで `human_review_required: true` を SubAgent が返した | `termination_reason: human_escalation` を立て、即停止 |
| Step 2 が `FAIL` または Step 4 が `REQUEST_CHANGES` で iteration 余裕あり | LOOP_STATE.iteration += 1、Step 1 に戻る（fix_delta を渡す）|

> **注意**: `verdict: APPROVE` 単独では `termination_reason: approved` に到達しない。
> `merge_ready == true` かつ `required_auto_actions == []` の両条件が揃った場合のみ終了する。

## APPROVE 時の終了 gate（三段構成）

Step 4 から `verdict: APPROVE` を受け取った場合、以下の順で gate を評価する:

```
APPROVE gate:
  1. reviewed_head_sha 整合確認（step-5-mergeability-handling.md 参照）
     - 不一致 → Step 4 再実行（stale LOOP_VERDICT 検出）
  2. required_auto_actions gate:
     if required_auto_actions != []:
       → required_auto_action_result_routing で worker 委譲
       → 処理結果に応じて verification / PR review を再実行
       → 終了しない（ループ継続）
  3. merge_ready gate:
     if merge_ready != true:
       → step-5-mergeability-handling.md の routing に従う
       → 終了しない
  4. 全 gate pass → termination_reason: approved で終了
```

## required_auto_action_result_routing

`required_auto_actions` は **YAML array of objects** として parse する（string-list 扱いは禁止）。

`required_auto_actions` が空でない場合の routing テーブル:

```yaml
required_auto_action_result_routing:
  update_pr_body_hygiene:
    head_change_expected: false
    rerun:
      verification: false
      pr_review: true
  ensure_closing_keyword:
    head_change_expected: false
    rerun:
      verification: false
      pr_review: true
  update_branch:
    head_change_expected: true
    rerun:
      verification: true
      pr_review: true
  worker_status_failed:
    route: human_escalation
  worker_status_blocked:
    route: human_escalation
  worker_status_permission_blocked:
    route: human_escalation
  worker_status_stale_verdict:
    route: human_escalation
    note: "reviewed_head_sha が変わっており verdict が stale"
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
    route: "rerun verification and pr_review（head 変更有無に依存）"
    note: "ok でも rerun_required: true の場合は即終了しない"
```

### required_auto_actions_schema（object parse 仕様）

各 action object の必須フィールドと unknown 時の routing:

```yaml
required_auto_actions_schema:
  type: array-of-objects
  item_schema:
    kind:
      allowed_values:
        - update_branch
        - update_pr_body_hygiene
        - ensure_closing_keyword
    executor:
      allowed_values:
        - implementation-worker
    skill: "<skill name>"
    blocking_merge_ready:
      allowed_values: [true, false]
    expected_head_sha: "<SHA> (required when kind == update_branch)"
  unknown_kind_route: human_escalation
  unknown_executor_route: human_escalation
  missing_expected_head_sha_for_update_branch: human_escalation
  blocking_merge_ready_not_true_route: human_escalation
```

`unknown kind` / `unknown executor` / `unknown skill` / `blocking_merge_ready != true` / `update_branch` で `expected_head_sha` 欠落のいずれかに該当する場合は `human_escalation` として停止する。

### 処理手順

1. `required_auto_actions` の各 action を `implementation-worker` に委譲する（child-4 / #631 で実装予定の mode を使用）
2. worker result の `status` で分岐する:
   - `failed` / `blocked` / `permission_blocked` → `termination_reason: human_escalation` で停止
   - `ok` → `head_change_expected` に応じて verification / PR review を再実行
3. body-only actions（`update_pr_body_hygiene` / `ensure_closing_keyword`）は head SHA 不変のため verification は省略し、PR review のみ再実行する
4. `update_branch` は head SHA が変化するため verification と PR review の両方を再実行する
5. `reviewed_head_sha` が現在 head と不一致の場合、dispatch 前に PR review を再実行する
6. 再実行後に得られた新 LOOP_VERDICT で再度 APPROVE gate を評価する

### docs-only mode 制約（child-4 / #631 OPEN 中）

child-4（#631）が OPEN の間、`implementation-worker` への dispatch 実行コードは追加しない。
本ドキュメントは **docs-only mode** として routing ロジックを記述するにとどめる。
実際の dispatch は #631 完了後に実装する。

## branch publish の deterministic retry / safety stop（決定的な再試行と安全停止）

branch publish が hook / approval 境界または remote head drift で止まった場合、`gh pr create` の再試行前に次の read-only preflight を必須とする。

1. `git ls-remote --refs --exit-code origin refs/heads/<branch>` または GitHub Branch API で live remote head を読む
2. local remote-tracking ref を使う場合は、同一 decision cycle 内の fetch 成功を `remote_readback_source: fetch_then_show_ref` として記録する
3. `expected_remote_head`、`current_remote_head`、`local_head`、`verified_head`、`declared_publish_head`、`allowed_paths_gate_status`、`remote_readback_source`、`decision_inputs_complete` を `PUBLISH_LANE_DECISION_V1` で照合する
4. `status: allow_retry` の場合だけ bounded publish command を再試行する
5. 不一致時は `PUBLISH_SAFETY_STOP_REPORT_V1` を残し、manual remote update や force update に暗黙フォールバックしない

strict publish lane を hook 側で再利用する場合は、以下の env binding を与える。

```yaml
LOOP_PUBLISH_EXPECTED_REMOTE_HEAD: "<sha>"
LOOP_PUBLISH_CURRENT_REMOTE_HEAD: "<sha>"
LOOP_PUBLISH_DECLARED_PUBLISH_HEAD: "<sha>"
LOOP_PUBLISH_VERIFIED_HEAD: "<sha>"
LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS: "ok|fail_closed|indeterminate"
LOOP_PUBLISH_REMOTE_READBACK_SOURCE: "ls_remote|github_branch_api|fetch_then_show_ref"
```

```yaml
PUBLISH_LANE_DECISION_V1:
  status: allow_retry | safety_stop
  publish_failure_reason:
    boundary_layer: worktree_scope_guard_denied
    reason_code: remote_write_requires_approval | branch_mismatch | stale_remote_head | local_head_mismatch | remote_head_scope_contamination | non_fast_forward_remote_rewrite | allowed_paths_gate_not_ok | publish_guard_context_missing | publish_guard_context_invalid
  remote_readback_source: ls_remote | github_branch_api | fetch_then_show_ref
  decision_inputs_complete: true | false
  allowed_command: null | "<bounded publish command>"
  postcondition: "remote branch head == local_head"
```

## merge_ready 要件

`merge_ready: true` は `merge_state_status == CLEAN` の場合のみ認める:

| merge_state_status | merge_ready | github_merge_ready | 備考 |
|---|---|---|---|
| `CLEAN` | `true` | `true` | |
| `DRAFT` | `false` | `false` | Draft PR は人間が ready にする |
| `HAS_HOOKS` | `true` | `true` | merge hooks があるが merge 可能 |
| `UNSTABLE` | `false` | 人間判断 | branch protection テスト失敗の可能性 |
| `BEHIND` | `false` | — | `step-5-mergeability-handling.md` の BEHIND 分岐参照 |
| `BLOCKED` | `false` | 人間判断 | branch protection 設定待ち |
| `DIRTY` / `CONFLICTING` | `false` | — | CONFLICTING PR Escalation Runbook 発動 |
| `UNKNOWN` | `false` | — | 5 秒待機 × 最大 3 回 retry 後も UNKNOWN なら `human_escalation` |

`UNSTABLE` は branch protection でのテスト失敗を示す場合があり、自動的に `merge_ready: true` とは見なさない。

### draft_pr_ready と github_merge_ready の区別

`IMPL_REVIEW_LOOP_RESULT_V1` では以下の 2 フィールドを分離して記録する:

- `draft_pr_ready: true` = protocol contract が満たされた状態（LOOP_VERDICT が APPROVE かつ全 gate pass）
- `github_merge_ready: true` = GitHub 側で実際に merge 可能な状態（`merge_state_status` が `CLEAN` または `HAS_HOOKS`）

Draft PR（`merge_state_status == DRAFT`）の場合:

```yaml
IMPL_REVIEW_LOOP_RESULT_V1:
  status: draft_pr_ready
  draft_pr_ready: true
  github_merge_ready: false  # Draft PR のため
  github_merge_state_status: DRAFT
  human_next_action: mark_ready_for_review_or_merge
```

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

## 終了処理（approved）

```bash
# LOOP_STATE を最終 YAML として会話履歴に記録
# PR は人間がマージ判断（orchestrator はマージしない）

# Issue コメントで終了報告（機械可読フィールドを含む）
gh issue comment <issue_number> --body "## impl-review-loop: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <最終 iteration 数>
- verdict: APPROVE
- merge_ready: true
- required_auto_actions: []
- PR: <PR URL>
- 次アクション: 人間レビュー → マージ → post-merge-cleanup

\`\`\`yaml
IMPL_REVIEW_LOOP_RESULT_V1:
  schema_version: 1
  status: draft_pr_ready
  merge_ready: true
  pr_url: \"<PR URL>\"
  head_sha: \"<HEAD SHA>\"
  issue_number: <ISSUE NUMBER>
  termination_reason: approved
  iteration: <最終 iteration 数>
  required_auto_actions: []

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

`LOOP_VERDICT.follow_up_issue_requests` が空でない場合、main thread は APPROVE 確定直後に各リクエストを `issue-author` SubAgent に委譲して `create-issue` 経由で **即時自動起票** する。

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
   - 各エントリを `severity: mandatory_follow_up` の `FOLLOW_UP_ISSUE_REQUEST_V1` として `LOOP_VERDICT.follow_up_issue_requests` に追加する
   - dedupe_key は `CHILD_MATERIALIZATION_PLAN_V2.children[*].dedupe_key` を使用する

3. `action: reuse_and_update_parent` のエントリがある場合:
   - `edit-issue` skill の `delivery-rollup-parent-update` mode に委譲して parent body の placeholder を修正する

4. `action: human_escalation` のエントリがある場合:
   - `human_review_required: true` で停止し、人間判断を仰ぐ

スキーマ: `CHILD_MATERIALIZATION_PLAN_V2` の正本は `docs/dev/agent-skill-boundaries.md` を参照。

pr-review-judge が `LOOP_VERDICT` の `follow_up_issue_requests` フィールドに格納した non-blocker NOTE（任意改善提案・観察事項）が起票対象となる。詳細スキーマは `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_ISSUE_REQUEST_V1` を参照。

```
for each req in LOOP_VERDICT.follow_up_issue_requests:
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
- 詳細: <SubAgent が返した human_review_required の理由>
- PR: <PR URL>
- 人間の確認後、ループ再開または別アプローチを選択してください"
```

## Publish Failure Safety Lane（publish 失敗時の安全レーン）

implementation-worker / open-pr が branch publish 境界で停止した場合、CI 結果や手動 remote 更新の事後成功だけで安全扱いしてはならない。
以下の順序で readback し、`PUBLISH_LANE_DECISION_V1` または `PUBLISH_SAFETY_STOP_REPORT_V1` を残す。

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
    reason_code: remote_write_requires_approval | hook_policy_denied | branch_mismatch | stale_remote_head | local_head_mismatch | remote_head_scope_contamination | non_fast_forward_remote_rewrite | allowed_paths_gate_not_ok | publish_guard_context_missing | publish_guard_context_invalid
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

## ISSUE_SCOPE_ROLLUP_DECISION_V2 の常時記録

`ISSUE_SCOPE_ROLLUP_DECISION_V2` は、統合を実施した場合・しなかった場合を問わず、
**ループの全終了経路（approved / max_iterations / human_escalation）で必ず記録する**。

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
- preparation Step 2.5 で scope rollup preflight を実行しなかった場合でも `decision: skipped` として記録する。
- `candidates_reviewed` は空配列（`[]`）でも記録する（候補なしの場合）。
- この記録を省略してはならない（MUST NOT skip）。

## Output（出力）

各終了条件に応じた LOOP_STATE 最終 YAML を会話履歴に記録する。

```yaml
LOOP_STATE:
  ...（全フィールド）
  iteration: <最終 iteration 数>
  last_step: judgment
  termination_reason: approved | max_iterations | human_escalation
  merge_ready: true | false | null
  required_auto_actions: []
  scope_rollup_decision: <ISSUE_SCOPE_ROLLUP_DECISION_V2>
```

その後、orchestrator は次のユーザー入力を待つ（自動で次イテレーションに進む決定済みなら Step 1 を再呼び出し）。
