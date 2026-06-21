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

## ANCHOR_SCOPE_REFRAME_V1 — trusted anchor による scope delta 承認

scope delta（`new_in_scope_area` / `new_allowed_path_layer` / `new_unverifiable_ac`）が検知されたとき、OWNER / MEMBER / COLLABORATOR が以下の copy/paste template を Issue コメントとして投稿することで、scope delta を承認できる。

### copy/paste template

```yaml
schema_version: ANCHOR_SCOPE_REFRAME_V1
target:
  repo: squne121/loop-protocol
  issue_number: <ISSUE_NUMBER>
decision: approve_scope_delta
allowed_path_deltas:
  - "<新しい Allowed Path>"
rationale: "<scope 拡張の理由を明記する>"
required_rerun:
  - contract_review
  - refinement_preflight
  - allowed_paths_gate
```

### フィールド仕様

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `schema_version` | string | 必須 | `ANCHOR_SCOPE_REFRAME_V1` 固定 |
| `target.repo` | string | 必須 | `squne121/loop-protocol` 固定 |
| `target.issue_number` | integer ≥ 1 | 必須 | 対象 Issue 番号 |
| `decision` | enum | 必須 | `approve_scope_delta` のみ |
| `allowed_path_deltas` | string[] (minItems: 1) | 必須 | 承認する新規 Allowed Path |
| `rationale` | string | 必須 | scope 拡張の理由 |
| `required_rerun` | enum[] (minItems: 1) | 必須 | 再実行が必要な工程（`contract_review` / `refinement_preflight` / `allowed_paths_gate`） |

スキーマは `schemas/anchor_scope_reframe_v1.schema.json` で JSON Schema Draft 2020-12 として管理する（`required` / `additionalProperties: false` / `enum` / `const` で固定）。

### 信頼境界（trusted anchor 判定）

GitHub API の `author_association` フィールドで判定する。

| `author_association` | 信頼 |
|---|---|
| `OWNER` | trusted |
| `MEMBER` | trusted |
| `COLLABORATOR` | trusted |
| `CONTRIBUTOR` | **fail-closed** |
| `NONE` | **fail-closed** |
| 未取得 / metadata 欠落 | **fail-closed** |

追加の fail-closed 条件:

- `target.issue_number` が実行中 issue と不一致
- `target.repo` が実行中 repo と不一致
- anchor URL が複数（単一コメントのみ信頼）
- schema が malformed（`additionalProperties: false` 違反 / enum 不一致等）
- comment body が quoted markdown（blockquote `>`）内に埋め込まれている
- fenced-code block 内の marker がさらに別の fenced-code や blockquote に入れ子になっている
  - **注意**: top-level の `\`\`\`yaml` ブロックが canonical format。blockquote の後の fence や、非 YAML fenced block は fail-closed。

trusted anchor と判定された場合のみ `scope_delta_decision.status=approved_by_trusted_anchor` を生成する。scope 拡張の自動実装許可は禁止。

### phase-sensitive semantics

anchor reframe は refinement loop の phase によって異なる扱いをする。

| phase | 挙動 |
|---|---|
| `preflight` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `investigation` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `review` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `post_rewrite_check` | hard stop 判定する |
| `decide_next_action` | hard stop 判定する |

### scope_delta_decision スキーマ（planner output）

```yaml
scope_delta_decision:
  status: approved_by_trusted_anchor | not_applicable | fail_closed
  anchor_comment_url: <url | null>
  anchor_comment_hash: <sha256 | null>
  anchor_author_association: OWNER | MEMBER | COLLABORATOR | null
  allowed_path_deltas: []
  required_rerun: []
  implementation_go: false
```

`implementation_go` は trusted anchor が approve した場合でも `false`。scope 拡張承認は実装開始の自動許可ではない。contract review / refinement preflight / allowed_paths_gate の再実行が必要。

## Must not

- scope signal を見て自動で scope 拡大を承認しない
- planner 判定を SKILL.md にハードコードしない
- raw anchor comment body を planner input に流さない（normalized decision / hash / provenance のみ渡す）
- `CONTRIBUTOR` / `NONE` を trusted anchor として扱わない
- phase-sensitive routing を bypass して hard stop を早期発火させない
