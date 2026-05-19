# Step 1: Implementation

`implementation-worker` SubAgent に委譲し、`implement-issue` skill の手順を実行させる。

## 委譲呼び出し

Agent ツールで以下を呼ぶ:

```
subagent_type: implementation-worker
inputs:
  issue_number: <LOOP_STATE.issue_number>
  contract_snapshot_url: <LOOP_STATE.contract_snapshot_url>
  fix_delta: <前イテレーションの blockers_history、初回 iteration では null>
```

SubAgent 側は `.claude/skills/implement-issue/SKILL.md` を実行し、worktree 作成・実装・検証・PR 起票（`open-pr` skill 経由）まで完了させる。

## 入力 (fix_delta)

REQUEST_CHANGES から戻ってきた場合、orchestrator は LOOP_STATE.blockers_history の最新エントリを `fix_delta` として渡す:

```yaml
fix_delta:
  iteration: <int>
  blockers:
    - "<blocker 1 の内容>"
    - "<blocker 2 の内容>"
  pr_review_comment_url: <URL>
```

implementation-worker は fix_delta を読み取り、該当箇所のみ修正する（スコープ拡大禁止）。

## 期待する出力

`IMPLEMENT_RESULT_V1` YAML（`implement-issue` SKILL.md の Output Contract 参照）:

```yaml
IMPLEMENT_RESULT_V1:
  status: ok | failed | blocked
  pr_url: <URL>
  worktree: <path>
  branch: <name>
  verification:
    typecheck: pass | fail
    lint: pass | fail
    test: {passed: <N>, failed: <N>, files: <N>}
    build: pass | fail
  allowed_paths_compliance: true | false
```

## エラー処理

| status | 次アクション |
|---|---|
| `ok` | LOOP_STATE.last_step = "implementation" に更新、Step 2 へ |
| `failed` | LOOP_STATE.blockers_history に記録、iteration をインクリメント、Step 1 を再委譲（同イテレーション内 retry） |
| `blocked` | 即停止、human_review_required として人間判断 |

3 回連続 `failed` で `termination_reason: human_escalation` を立てて停止。
