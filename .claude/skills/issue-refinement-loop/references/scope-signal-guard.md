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

## Phase-sensitive scope_signal_guard semantics

`scope_signal_guard.triggered` の意味は現在の **refinement phase** によって異なる。
Phase は `ISSUE_REFINEMENT_PHASE_STATE_V1` の `scope_signal_semantics` が定義する。

| phase | triggered_meaning | hard_stop_eligible | 動作 |
|---|---|---|---|
| `preflight` | `continue_investigation` | **false** | signal 検出 → investigation/web_research/review へ進む。`decide_next_loop_action.py` を呼ばない |
| `investigation` | `continue_investigation` | **false** | signal 検出 → 継続。hard stop にならない |
| `review` | `continue_investigation` | **false** | pre-rewrite phase。`decide_next_loop_action.py` を呼ばない。routing は VERDICT に基づいて直接行う |
| `post_rewrite_check` | `hard_stop_candidate` | **true** | signal in post-rewrite → `human_escalation` |
| `decide_next_action` | `hard_stop_candidate` | **true** | signal in routing → `human_escalation` |
| `rewrite` | `ignored` | false | rewrite 中は signal を無視 |
| `publish` / `terminate` | `ignored` | false | publish/terminate 中は signal を無視 |

### hard_stop_eligible の判定条件

`hard_stop_eligible: true` となる条件:

1. `ISSUE_REFINEMENT_PHASE_STATE_V1.scope_signal_semantics.hard_stop_eligible == true`
2. かつ `scope_signal_guard.triggered == true`
3. かつ `scope_signal_guard.excluded_by_anchor_reframe == false`

**Phase gate ルール**: `hard_stop_eligible: false` の phase では、`decide_next_loop_action.py` を
scope_signal_guard の評価のために呼んではならない。orchestrator は planner の
`investigation_policy` / `web_research_policy` に従って次のステップを決定する。

### preflight phase での誤 routing 防止

`run_refinement_preflight.py` が `STATUS: pass` を返しても、その結果に
`scope_signal_guard.triggered: true` が含まれることがある（planner が signal を検知しただけで
hard stop ではない状態）。

このとき、orchestrator が誤って `decide_next_loop_action.py` を呼ぶと `human_escalation` になる
（これが修正前のバグパターン）。

正しい動作:

1. preflight / investigation / review phase では `decide_next_loop_action.py` を呼ばない（allowed_routers に含まれない）
2. `scope_signal_guard.triggered: true` は `continue_investigation` として扱う
3. planner の `investigation_policy` / `web_research_policy` に従って Step 1 / Step 1b / Step 2 へ進む
4. `decide_next_loop_action.py` は `post_rewrite_check` / `decide_next_action` phase（`hard_stop_eligible: true`）でのみ呼ぶ

Phase gate は `decide_next_loop_action.py` の `--phase-state-file` 引数で実施する:

```bash
uv run python3 .claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py \
  --loop-state-file <path> \
  --review-result-verdict <verdict> \
  --phase-state-file <path/to/ISSUE_REFINEMENT_PHASE_STATE_V1.json>
```

preflight phase で呼んだ場合は `ISSUE_REFINEMENT_ROUTER_ERROR_V1` を返して
`NEXT_ACTION: rebuild_phase_state` で終了する（人間への誤 escalation にならない）。

## orchestrator への termination_cause 正規化注記

`scope_signal_guard.triggered=true` かつ `hard_stop_eligible: true` で停止する場合、
orchestrator は `decide_next_loop_action.py` の出力から `TERMINATION_CAUSE: human_judgment_required`
を読み取り、termination payload の `termination_cause` に使用する。

`scope_signal_guard.reason_code`（例: `new_allowed_path_layer`）は diagnostic code であり、
`termination_cause` として render/publish に渡してはならない。`reason_code` は BLOCKERS から
`blockers_summary` に転記し、終了コメントで確認可能な状態にする。

詳細手順は `references/termination-policy.md` の「scope_signal_guard 停止時の termination payload 正規化」セクションを参照する。

## Must not

- scope signal を見て自動で scope 拡大を承認しない
- planner 判定を SKILL.md にハードコードしない
- preflight / investigation / review phase で `decide_next_loop_action.py` を呼ぶ（allowlist gate により forbidden）
- `hard_stop_eligible: false` の phase で scope signal を hard stop として扱う
