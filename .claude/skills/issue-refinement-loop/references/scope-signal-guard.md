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

## Loop stop signals

iteration 中に以下が新規追加されたら `termination_reason: human_escalation` で停止する。

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に別アーキテクチャ層が追加された
- `## Acceptance Criteria` に低検証可能 AC が追加された

## Must not

- scope signal を見て自動で scope 拡大を承認しない
- planner 判定を SKILL.md にハードコードしない
