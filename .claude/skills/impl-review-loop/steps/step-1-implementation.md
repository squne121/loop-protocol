# Step 1: Implementation

`implementation-worker` SubAgent に委譲し、`implement-issue` skill の手順を実行させる。
root/main（root thread）は最終 routing と mutation authorization を保持し、明示的に委譲された mechanical executor だけが既存の bounded contract 内で実行する。

## 委譲呼び出し

Agent ツールで以下の static call shape を使って起動する:

```yaml
spawn_agent:
  task_name: implementation_i{iteration}
  agent_type: implementation-worker
  fork_turns: none
  message: |
    Objective: execute Step 1 implementation through implement-issue for the actual live Issue.
    Live reference: bind the actual Issue number, full Issue URL, and contract snapshot URL when supplied.
    Bounded scope: bind the actual live Allowed Paths and serialized fix_delta only.
    Expected result: IMPLEMENT_RESULT_V1 with the actual worktree, branch, PR, and verification facts.
```

This dispatch block defines static call shape only. It does not prove runtime capability, permission enforcement, or security-boundary verification. Native runtime verification is owned by #1841. この静的な記述は実行時の能力・権限強制・security boundary を証明しません。

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `implementation_i{iteration}` から materialize し、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の Issue number、完全な Issue URL、contract snapshot URL（指定された場合）、Allowed Paths、serialized `fix_delta` を値として埋め込む。`LOOP_STATE.issue_number`、変数名、`current`、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

完了の扱いは4 site 共通の [Common Completion Protocol](step-4-pr-review.md#common-completion-protocol) に従う。

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
