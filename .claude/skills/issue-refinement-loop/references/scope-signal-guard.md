# Scope Signal Guard

## Planner boundary

scope signal の検知は `plan_refinement_loop.py` が生成する `REFINEMENT_LOOP_PLAN_V1.decisions.scope_signal_guard` を SSOT とする。orchestrator は `triggered` / `excluded_by_anchor_reframe` / `reason_code` を consume するだけで、判定条件を prose 再実装しない。

## Scope rollup preflight

同一 skill family / Allowed Paths / parent issue の衝突確認は `scope-rollup-policy.md` を参照する。rollup の候補が `human_review_required` の場合は即停止する。

## Product/Spec routing summary

Issue title / body / labels に `docs/product/**`、`tasks.md`、`.specify/`、`spec.md`、`plan.md`、`speckit` 系 token がある場合は `product_spec_context` を更新する。

- `tasks.md` シグナルあり: `work_kind: tasks_materialization` とし、implementation route へ進めない
- spec / plan / specify signal あり: `spec_creation` または `spec_update` として routing hint を記録する
- `docs/product/**` 単独: `unknown` として扱い、後続 worker に context を渡す

最低限維持すべき fail-closed routing state は以下。

```yaml
product_spec_routing_gate:
  tasks_md_signal:
    work_kind: tasks_materialization
    routing_target: issue_materialization
    fail_closed: true
    implementation_route_allowed: false
```

`tasks.md` signal がある場合は `LOOP_STATE.product_spec_context.work_kind = tasks_materialization` を設定し、implementation route へ進めない。routing 先は `issue_materialization` として記録する。

## Loop stop signals

iteration 中に以下が新規追加されたら `termination_reason: human_escalation` で停止する。

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に別アーキテクチャ層が追加された
- `## Acceptance Criteria` に低検証可能 AC が追加された


## orchestrator への termination_cause 正規化注記

`scope_signal_guard.triggered=true` で停止する場合、orchestrator は `decide_next_loop_action.py` の出力から `TERMINATION_CAUSE: human_judgment_required` を読み取り、termination payload の `termination_cause` に使用する。

`scope_signal_guard.reason_code`（例: `new_allowed_path_layer`）は diagnostic code であり、`termination_cause` として render/publish に渡してはならない。`reason_code` は BLOCKERS から `blockers_summary` に転記し、終了コメントで確認可能な状態にする。

詳細手順は `references/termination-policy.md` の「scope_signal_guard 停止時の termination payload 正規化」セクションを参照する。

## Must not

- scope signal を見て自動で scope 拡大を承認しない
- planner 判定を SKILL.md にハードコードしない
