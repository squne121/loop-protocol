# スコープシグナルガード（Scope Signal Guard）

## planner の日本語境界説明（Planner boundary）

scope signal の検知は `plan_refinement_loop.py` が生成する `REFINEMENT_LOOP_PLAN_V1.decisions.scope_signal_guard` を SSOT とする。orchestrator は `triggered` / `excluded_by_anchor_reframe` / `reason_code` を consume するだけで、判定条件を prose 再実装しない。

`scope_signal_delta.py` を使う path では、planner は `known_context.scope_signal_delta_input` から受け取った `before/current/after` の normalized delta facts を consume し、legacy `scope_signal_guard` projection だけを `REFINEMENT_LOOP_PLAN_V1` に反映する。raw anchor comment body を planner や delta helper の入力へ直接流してはならない。

`known_context.scope_signal_delta_input` が存在する場合は fail-closed で consume する。helper の欠落、必須 field 欠落、未知 field、`source_refs` 不整合、その他 malformed input を旧 prefix heuristic へ fallback してはならない。

## scope rollup 事前確認（Scope rollup preflight）

同一 skill family / Allowed Paths / parent issue の衝突確認は `scope-rollup-policy.md` を参照する。rollup の候補が `human_review_required` の場合は即停止する。

## Product/Spec ルーティング要約（routing summary）

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

## ループ停止シグナル（Loop stop signals）

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

### コピペ用テンプレート（copy/paste template）

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

### phase ごとの扱い（phase-sensitive semantics）

anchor reframe は refinement loop の phase によって異なる扱いをする。

| phase | 挙動 |
|---|---|
| `preflight` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `investigation` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `review` | hard stop しない。`scope_delta_decision` を記録して通過 |
| `post_rewrite_check` | hard stop 判定する |
| `decide_next_action` | hard stop 判定する |

### scope_delta_decision スキーマ（planner output 契約）

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

## scope_signal_delta 入力契約（input contract）

```yaml
scope_signal_delta_input:
  before_body: string
  current_body: string
  after_body: string
  source_refs:
    before: string | null
    current: string | null
    after: string | null
```

- `before_body`: rewrite 前の canonical issue body
- `current_body`: planner / checker が評価対象として読む current snapshot body
- `after_body`: proposed rewrite body または fixture が与える candidate body
- `source_refs.*`: issue URL / artifact path / fixture path / comment id の provenance
- planner が projection を `evidence_spans` へ写す場合も、`body_version` に対応する `source_ref` を保持する

`new_allowed_path_layer` は `after.allowed_path_layers - before.allowed_path_layers` が非空の場合のみ発火する。既存 layer の再掲、並び替え、空白差分、fenced code 内の path mention は signal にしない。

## 禁止事項（Must not）

- scope signal を見て自動で scope 拡大を承認しない
- planner 判定を SKILL.md にハードコードしない
- raw anchor comment body を planner input に流さない（normalized decision / hash / provenance のみ渡す）
- `CONTRIBUTOR` / `NONE` を trusted anchor として扱わない
- phase-sensitive routing を bypass して hard stop を早期発火させない


## SCOPE_SIGNAL_GUARD_DECISION_V2（エスカレーション lane 分割契約, #1090）

`plan_refinement_loop.py` は `known_context.scope_signal_delta_input` が与えられた場合に限り、
`REFINEMENT_LOOP_PLAN_V1` のトップレベルに追加フィールド `scope_signal_guard_decision_v2` を出力する
（既存の `decisions.scope_signal_guard` の意味・値は変更しない。additive のみ）。

```yaml
scope_signal_guard_decision_v2:
  schema_version: SCOPE_SIGNAL_GUARD_DECISION_V2
  raw_signal:
    triggered: true | false
    reason_code: new_in_scope_area | new_allowed_path_layer | new_unverifiable_ac | anchor_reframe_exclusion | no_scope_signal
  scope_context:
    path_layer: [runtime | docs | skill | hook | agent | test_fixture | unknown]
  scope_delta_approval:
    present: true | false
    valid: true | false
    status: missing | missing_marker | invalid_scope_delta_approval | approved
    missing_approval_field: true | false
    suggested_contract_patch: <string | null>
    comment_id: <int | null>
    comment_url: <string | null>
    body_sha256: <sha256 | null>
    author_association: OWNER | MEMBER | COLLABORATOR | null
    created_at: <ISO-8601 | null>
    issue_url: <string | null>
  security_sensitive: true | false
  route: proceed_with_notes | human_judgment_required | security_risk_gate_required | invalid_scope_delta_approval | not_triggered
```

### scope_context.path_layer 分類（AC1）

`_classify_path_layer()` は Allowed Paths delta（`scope_signal_delta.py` が返す `sections.allowed_paths.added`）
の各パスを以下の優先順位で分類する。

| 優先順位 | prefix | path_layer |
|---|---|---|
| 1 | `.claude/hooks/` | `hook` |
| 2 | `.claude/agents/` | `agent` |
| 3 | `.claude/skills/` | `skill` |
| 4 | `docs/` | `docs` |
| 5 | `src/` | `runtime` |
| 6 | `tests/` / `fixtures/`（部分一致含む） | `test_fixture` |
| — | 上記いずれにも一致しない | `unknown` |

### route 決定ロジック（AC2/AC3/AC4/AC8/AC9/AC13）

`_decide_scope_signal_route()` は以下の優先順位で route を決定する。

1. `raw_signal.triggered=false` → `not_triggered`
2. security-sensitive path/term を含む → `security_risk_gate_required`（approval があっても override 不可、AC13）
3. Scope Delta Approval が存在しない、または marker（`ANCHOR_SCOPE_REFRAME` / `Scope Delta Approval` / `Allowed Paths Expansion Rationale`）が確認できない → `human_judgment_required`（AC3）
4. Scope Delta Approval は存在するが対象 Issue 不一致（AC8）または `author_association` が信頼できない（AC9） → `invalid_scope_delta_approval`
5. 上記いずれにも該当しない（trusted anchor による有効な approval） → `proceed_with_notes`（AC2/AC4/AC12。実装 go ではなく contract-review rerun required）

### security-sensitive fail-closed gate（セキュリティ機微判定, AC13）

`_is_security_sensitive_scope_delta()` は以下を deterministic に判定する（#558 の security gate 本体とは別の、狭い fail-closed チェック）。

- 追加パスが `.claude/hooks/` または `.github/workflows/` から始まる
- 追加パス文字列、または承認 evidence の `rationale` に `secret` / `token` / `permission` / `credential` のいずれかが含まれる（大文字小文字を区別しない）

該当する場合は Scope Delta Approval の有無・valid/invalid に関わらず `security_risk_gate_required` を返す。

### missing approval の診断情報（AC6）

`scope_delta_approval.missing_approval_field=true` のとき、`suggested_contract_patch` に
「OWNER/MEMBER/COLLABORATOR が ANCHOR_SCOPE_REFRAME コメントを投稿する」ことを促す定型文を返す。
承認が valid な場合（`status: approved`）は `suggested_contract_patch: null`。

### known_context.scope_delta_approval_evidence 入力契約（AC8/AC9/AC10）

`scope_signal_guard_decision_v2` の承認判定は、raw anchor comment body を直接読まない。
呼び出し側（orchestrator/checker）が以下の正規化済み evidence を `known_context.scope_delta_approval_evidence`
として渡す。

```yaml
scope_delta_approval_evidence:
  marker_present: true | false        # ANCHOR_SCOPE_REFRAME 系 marker を検出済みか
  target_issue_number: <int>          # コメントが投稿された Issue 番号
  author_association: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | null
  comment_id: <int | null>
  comment_url: <string | null>        # 対象 Issue のコメント URL のみ有効（AC8）
  body_sha256: <sha256 | null>
  created_at: <ISO-8601 | null>
  issue_url: <string | null>
  rationale: <string | null>          # security-sensitive term 判定にも使用
```

`target_issue_number` が実行中の Issue と一致しない場合（別 Issue / PR / 外部 URL 由来のコメント）は
`invalid_scope_delta_approval` として fail-closed になる（AC8）。`author_association` が
`OWNER` / `MEMBER` / `COLLABORATOR` のいずれでもない場合も同様に fail-closed（AC9）。

### opt-in ガード（既存 golden fixture 非破壊）

`scope_signal_guard_decision_v2` は `known_context.scope_signal_delta_input` が与えられた場合にのみ
`REFINEMENT_LOOP_PLAN_V1` に追加される。これにより `schemas/refinement_loop_plan_v1.json`
（本 Issue の Allowed Paths 外）を変更せずに additive な出力拡張を実現している。
`scope_signal_delta_input` を使わない既存呼び出し元は、この新フィールドを一切目にしない。
