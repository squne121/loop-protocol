# Step 2: Verification

Step 1 で PR が起票されたら、`test-runner` SubAgent に検証を委譲する。

Codex CLI: spawn the custom agent named test-runner for this step; the root thread must not edit files, run tests, commit, push, or make the review judgment directly.

## 委譲呼び出し

Agent ツールで以下を呼ぶ:

```
subagent_type: test-runner
inputs:
  issue_number: <LOOP_STATE.issue_number>
  pr_number: <Step 1 で取得した PR 番号>
  ac_list: <linked issue の Acceptance Criteria 一覧>
  verification_commands: <linked issue の Verification Commands>
```

SubAgent 側は `.claude/agents/test-runner.md` の手順を実行し、Verification Commands を実行して `TEST_VERDICT_MACHINE v1` マーカー付きコメントを PR に投稿する。

## 受け取り結果の期待値

test-runner が PR コメントに投稿する `TEST_VERDICT` YAML:

```yaml
TEST_VERDICT:
  result: PASS | PARTIAL | FAIL
  mergeable: MERGEABLE | CONFLICTING | UNKNOWN
  merge_state_status: CLEAN | UNSTABLE | BEHIND | DIRTY | BLOCKED | UNKNOWN
  baseline_only: true | false
  verification_commands_pass: <int>
  verification_commands_fail: <int>
```

`TEST_VERDICT` は Step 2 の実行結果を示すみにし、`baseline_only` は**routing の正本ではない**。
`baseline_only` は `adjudicate_vc_result.py` の evidence input としてのみ扱い、`VC_ADJUDICATION_RESULT_V1` の評価に渡す。

## 判定ルーティング

`VC_ADJUDICATION_RESULT_V1.overall_status` と `blocking` を contract snapshot + current VC + diff summary + allowed paths から生成し、Step 2 routing の正本にする。

`VC_ADJUDICATION_RESULT_V1` の `overall_status` / `blocking` が欠落、破損、期限切れである場合は fail-closed とし、Step 2 の判定は blocking とする。

判定表:

| TEST_VERDICT.result | LOOP 判定 | 次アクション |
|---|---|---|
| `PASS` | `overall_status == pass` かつ `blocking == false` | Step 3（pr-reviewer）へ |
| `PASS` | `VC_ADJUDICATION_RESULT_V1.blocking == true` | Step 2 エビデンス不足/再実行扱いとして再判定へ |
| `PARTIAL` | 任意 | Step 3 へ進むが `LOOP_STATE.blockers_history` に記録 |
| `FAIL` | 任意 | Step 5 へ直行し REQUEST_CHANGES として処理 |

## 追加注意: baseline_only

- `baseline_only: true` のみで Step 2 を PASS と見なさない。
- `baseline_only` は、VC 判定結果の `evidence_refs`/`source_integrity` を整えるための参照情報とし、`VC_ADJUDICATION_RESULT_V1` の routing 正本を上書きしない。
- `VC_ADJUDICATION_RESULT_V1` の生成に必要な証跡（`baseline`, `current`, `diff`, `allowed_paths`）が欠損している場合は fail-closed で blocking。

## BEHIND 状態の取り扱い

`merge_state_status: BEHIND` は「head ref が base branch より古い（base が先行している）」状態を意味し、`mergeable: MERGEABLE` と両立する。
`BEHIND` は `CONFLICTING / DIRTY / BLOCKED` と同一視しない。`CONFLICTING PR Escalation Runbook` の発動条件に該当しない。

`BEHIND` の場合、Step 2 では `update-branch` / `rebase` を実行しない。
branch の更新（`gh pr update-branch` 等）は Step 5 および `#67` の責務として分離されており、Step 2 はその実行を担わない。

## 出力

LOOP_STATE.last_step = "verification" に更新し、`VC_ADJUDICATION_RESULT_V1` を会話履歴に保持して次ステップへ。
