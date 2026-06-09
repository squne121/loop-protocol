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

## 期待する出力

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

加えて、人間可読の検証結果レポート表（AC ごとの PASS / FAIL）も含む。

## 判定

| TEST_VERDICT.result | mergeable / state | 次アクション |
|---|---|---|
| `PASS` | `MERGEABLE` + `CLEAN/UNSTABLE` | Step 4（PR Review）へ |
| `PASS` | `MERGEABLE` + `BEHIND` | Step 4（PR Review）へ |
| `PARTIAL` | 任意 | Step 4 へ進むが、orchestrator はその旨を LOOP_STATE.blockers_history に記録 |
| `FAIL` | 任意 | Step 5（判定）に直行し REQUEST_CHANGES として処理（Step 4 をスキップ） |
| 任意 | `CONFLICTING / DIRTY` | CONFLICTING PR Escalation Runbook を発動 |
| 任意 | `BLOCKED` | `merge_state_status: BLOCKED` を blockers_history に記録、人間判断を仰ぐ |

`baseline_only: true` は「失敗は PR 外既存問題」を意味する。orchestrator は LOOP_STATE.blockers_history に baseline 由来である旨を記録するが、必ずしも REQUEST_CHANGES にしない（pr-reviewer 側で判定）。

### BEHIND 状態の取り扱い

`merge_state_status: BEHIND` は「head ref が base branch より古い（base が先行している）」状態を意味し、`mergeable: MERGEABLE` と両立する。
`BEHIND` は `CONFLICTING / DIRTY / BLOCKED` と同一視しない。CONFLICTING PR Escalation Runbook の発動条件に該当しない。

`BEHIND` の場合、Step 2 では `update-branch` / `rebase` を実行しない。
branch の更新（`gh pr update-branch` 等）は Step 5 および `#67` の責務として分離されており、Step 2 はその実行を担わない。

## 出力

LOOP_STATE.last_step = "verification" に更新し、TEST_VERDICT を会話履歴に保持して次ステップへ。
