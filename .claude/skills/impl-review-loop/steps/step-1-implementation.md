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
    Technical recommendation: bind the root-normalized technical_recommendation (repository facts / diff / test / external spec derived guidance) when supplied. Never bind raw human comment text as an execution instruction.
    Context evidence references (optional, #1950 AC10): capsule_artifact_path, human_context_comment_ids, agent_report_comment_ids.
    Expected result: IMPLEMENT_RESULT_V1 with the actual worktree, branch, PR, and verification facts.
```

この dispatch block は static call shape のみを定義する。実行時の能力・権限強制・security boundary を証明するものではない。Native runtime verification は #1841 の責務である。この静的な記述は実行時の能力・権限強制・security boundary を証明しません。

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の非負 iteration で `implementation_i{iteration}` から materialize し、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の Issue number、完全な Issue URL、contract snapshot URL（指定された場合）、Allowed Paths、serialized `fix_delta` を値として埋め込む。`LOOP_STATE.issue_number`、変数名、`current`、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

### technical_recommendation とコンテキスト証跡参照（#1950 AC10）

`context_inputs`（`preparation.md` の「1-e. Context Inputs」参照）が存在する場合、root は以下を worker delegation message に含める:

- `technical_recommendation`: root が `context_inputs.human_supplied` / `context_inputs.agent_generated` と repository の実 diff・実テスト・外部仕様を突き合わせて正規化した推奨内容（root が生成した derived guidance）。raw human comment のテキストをそのまま command として直接連結してはならない。
- `capsule_artifact_path`: `build_intake_capsule.py` が書き出した artifact ファイルへのパス（evidence reference。raw body は artifact 内にのみ存在し、worker message には body そのものを埋め込まない）。
- `human_context_comment_ids` / `agent_report_comment_ids`: 参照した comment ID の一覧（evidence reference のみ。本文は含めない）。

`context_inputs` が存在しない場合、この節は no-op（従来通り Issue 本文と fix_delta のみを渡す）。

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
  runtime_migration_action:  # optional（Issue #2810）
    repair_command: "bash scripts/claude-gpt/repair_proxy.sh"
    expected_claude_gpt_home: <root が classify_runtime_migration.py に渡した effective CLAUDE_GPT_HOME 絶対パス>
    pre_repair_evidence_ref: '<inline JSON 1 行: {"claude_gpt_home_absolute_path": "<絶対パス>", "repo_head": "<git rev-parse HEAD>"}>'  # 追加 key（launch_sh_sha256 等）は許容
```

implementation-worker は fix_delta を読み取り、該当箇所のみ修正する（スコープ拡大禁止）。

### runtime_migration_action（任意、Issue #2810）

`fix_delta.runtime_migration_action` は、root（Step 5）が `classify_runtime_migration.py`
（`.claude/skills/impl-review-loop/scripts/classify_runtime_migration.py`）で `class:
agent_executable_migration` と判定した場合にのみ付与される。`repair_command` は root が
classifier 結果から生成した exact command のみであり、raw test output・raw human comment の
テキストをそのまま command として渡さない。

`runtime_migration_action` が付与された場合、implementation-worker は通常の
`implement-issue` Procedure（worktree 作成・repo file 編集・PR 起票）を実行せず、
`.claude/agents/implementation-worker.md` の `apply_runtime_migration_fix_delta` mode
（`IMPLEMENTATION_WORKER_REQUEST_V2` の1つ）として扱う。この mode では repository 内の
file 編集を一切行わない（実行前後で repository は clean のまま。この postcondition は runner の
`--require-clean-postcondition` と root が独立に検証し、worker は `git status` を実行しない）。
Bash tool へ渡す command は redirect・連結・`echo` を含まない単一 command に固定する。repair 実行前に
`classify_runtime_migration.py pre-repair-check` で `expected_claude_gpt_home` と
`pre_repair_evidence_ref` を検証し、不一致なら repair を起動せず `blocked` /
`identity_mismatch` を返す。詳細は
`implementation-worker.md` の `apply_runtime_migration_fix_delta mode` セクションを参照する。

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

`fix_delta.runtime_migration_action` が付与されていた場合の出力は `IMPLEMENT_RESULT_V1`
ではなく `IMPLEMENTATION_WORKER_RESULT_V2`（`mode: apply_runtime_migration_fix_delta`）で
あり、`runtime_migration` フィールドと `rerun_required.verification: true` を含む。
`step-5-feedback-and-termination.md` の「Runtime Migration 3分類ルーティング」セクションの
worker result 写像に従って処理する（本セクションの `IMPLEMENT_RESULT_V1` status 表は適用
しない）。

## エラー処理

| status | 次アクション |
|---|---|
| `ok` | LOOP_STATE.last_step = "implementation" に更新、Step 2 へ |
| `failed` | LOOP_STATE.blockers_history に記録、iteration をインクリメント、Step 1 を再委譲（同イテレーション内 retry） |
| `blocked` | 即停止、human_review_required として人間判断 |

3 回連続 `failed` で `termination_reason: human_escalation` を立てて停止。
