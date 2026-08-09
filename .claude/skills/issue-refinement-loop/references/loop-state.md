---
topic: loop_state
file: references/loop-state.md
loaded_when: need to understand LOOP_STATE field semantics or routing decisions
owner: issue-refinement-loop orchestrator
moved_from: SKILL.md##LOOP_STATE Summary
must_not: re-implement routing logic — use decide_next_loop_action.py
note_ja: このファイルは LOOP_STATE_V1 のフィールド定義とルーティング意味論を日本語で解説する。
---

# LOOP_STATE リファレンス

`LOOP_STATE_V1` の全フィールド定義とルーティング意味論。

#1873（bounded review loops）: `schemas/loop_state.schema.json` と、それを検証する
`build_loop_state.py` builder は撤去された。`LOOP_STATE_V1` は orchestrator が
planner（`plan_refinement_loop.py`）と review（`issue-reviewer` SubAgent）の結果から
直接組み立てる plain dict であり、`decide_next_loop_action.py` は `iteration` /
`max_iterations` / `scope_signal_guard`（optional）を含む最小限の構造チェックのみ行う
（`decide_next_loop_action.validate_loop_state()` 参照）。

## フィールド一覧

| field | type | routing_critical | description |
|---|---|---|---|
| `schema_version` | string const | no | `"loop_state/v1"` |
| `issue_number` | int | no | 対象 Issue 番号 |
| `iteration` | int (0-indexed) | yes | 現在のイテレーション数 |
| `max_iterations` | int (default 3) | yes | 上限。`iteration >= max_iterations` で人間へエスカレーション |
| `last_verdict` | `approve\|needs-fix\|null` | yes | 直近の review 判定 |
| `blockers_history` | array | yes | エスカレーション要約用の全イテレーション blocker リスト |
| `improvements_applied` | array of string | no | イテレーションごとの rewrite メモ |
| `removed_state_labels` | array of string | no | hygiene のため削除された label |
| `termination_reason` | enum\|null | yes | loop が終了した理由 |
| `scope_rollup_decision` | string\|null | yes | scope rollup preflight の出力 |
| `anchor_comment` | object | yes | anchor comment のスナップショットと分類 |
| `investigation_policy` | object | yes | コードベース調査が必要かどうか |
| `scope_signal_guard` | object | yes | scope 変更シグナルが検出されたかどうか |
| `web_research_policy` | object | yes | web research が必要かどうか |
| `web_research` | object | no | web research の実行状態 |
| `product_spec_context` | object | yes | Product/Spec 作業種別シグナル |
| `delivery_rollup` | object | yes | parent delivery rollup の適用可否 |
| `follow_up_materialization` | object | yes | follow-up issue 候補 |
| `superseded_decision` | object | yes | 本 Issue が人間判断により supersede された場合の情報 |

## orchestrator が LOOP_STATE_V1 を組み立てる際のソース

| LOOP_STATE_V1 field | Source |
|---|---|
| `issue_number` | 対象 Issue 番号 |
| `iteration` | orchestrator が管理する現在のイテレーション番号（0-indexed） |
| `max_iterations` | 既定 3（`decide_next_loop_action.py --max-iterations` で override 可） |
| `web_research_policy` | `REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy` |
| `scope_signal_guard` | `REFINEMENT_LOOP_PLAN_V1.decisions.scope_signal_guard` |
| `delivery_rollup` | `REFINEMENT_LOOP_PLAN_V1.decisions.delivery_rollup` |
| `follow_up_materialization` | `REFINEMENT_LOOP_PLAN_V1.decisions.follow_up_materialization` |
| `last_verdict` | `ISSUE_REVIEW_RESULT_COMPACT_V1.VERDICT` |
| `blockers_history` | orchestrator が iteration ごとに追記する blocker 要約リスト |
| `termination_reason` | loop 終了まで `null`（`decide_next_loop_action.py` の判定を受けて orchestrator が設定） |

## ルーティング意味論

### iteration / max_iterations（イテレーション数と上限）

`iteration` は `decide_next_loop_action.py` に渡される現在の 0-indexed ラウンド番号である。
次のラウンドが存在する限り継続可能: `iteration + 1 < max_iterations`。

| condition | next action |
|---|---|
| `last_verdict == approve` | `proceed_to_step_4_5`（child/follow-up materialization） |
| `last_verdict == needs-fix` かつ `iteration + 1 < max_iterations` | `continue_to_step_4`（rewrite） |
| `last_verdict == needs-fix` かつ `iteration + 1 >= max_iterations` | `human_escalation` |
| `termination_reason != null` | loop はすでに終了 — アクションなし |

### termination_reason の値

| value | meaning |
|---|---|
| `approved` | review が `approve` 判定を出した |
| `human_escalation` | `max_iterations` 超過、または hard stop シグナル |
| `superseded_by_decision` | 人間の anchor comment が loop を supersede した |
| `null` | loop はまだ終了していない |

### scope_rollup_decision（rollup 判断）

Step 0（イテレーション開始前）で設定される。非 null の場合、orchestrator は rollup 判断を記録するが停止しない — rollup が advisory であれば planner は処理を継続してよい。

### scope_signal_guard（scope 変更シグナル）

| field | meaning |
|---|---|
| `triggered` | scope 変更シグナルが検出された |
| `excluded_by_anchor_reframe` | シグナルが anchor comment reframe により除外された |
| `reason_code` | planner からの詳細な理由コード |

`scope_signal_guard.triggered` は **呼び出しタイミングに依存する**。#1873 で
`ISSUE_REFINEMENT_PHASE_STATE_V1`（`build_refinement_phase_state.py` が生成する
formal phase-gate）は撤去された — 代わりに orchestrator 自身が SKILL.md の Step
順序（フロー構造そのもの）に従って `decide_next_loop_action.py` を呼ぶタイミングを
制御する。

| フロー上の位置 | `decide_next_loop_action.py` を呼ぶか | effect |
|---|---|---|
| preflight / investigation | 呼ばない | シグナルは investigation/review へ進む合図として扱われるのみ |
| review（rewrite 前） | 呼ばない | VERDICT に基づき直接ルーティングする |
| rewrite 後 / next-action 決定時 | 呼ぶ | `scope_signal_guard.triggered == true` かつ `excluded_by_anchor_reframe == false` の場合、`decide_next_loop_action.py` は無条件で `human_escalation` を返す（`decide_next_action()` Priority 3） |
| rewrite / publish / terminate 中 | 呼ばない | シグナルは無視される |

`decide_next_loop_action.py` は呼ばれた時点で常に scope_signal_guard の hard-stop
判定を行う（呼び出しタイミングを制御するのは orchestrator の責務であり、スクリプト
自身は phase 概念を持たない）。シグナルの分類ルールについては
`references/scope-signal-guard.md` を参照。

### delivery_rollup（配送 rollup）

| field | meaning |
|---|---|
| `applicable` | 本 Issue が delivery-rollup parent issue である |
| `unmaterialized_slots` | まだ作成されていない child issue slot |

`applicable == true` かつ `unmaterialized_slots` が非空の場合、orchestrator は Step 4.5 で
終了前に child materialization を行う。

### follow_up_materialization（follow-up 具体化）

`candidates` は follow-up issue 提案のリストである。重複排除は `dedupe_key`（title ではない）を使う。
候補は承認後に Step 4.5 で materialize される。

### superseded_decision（supersede 判断）

人間の anchor comment が loop を supersede した場合（例: Issue を won't fix としてクローズ、または
代替案へリダイレクト）、`superseded_decision` がその要約を保持する。loop は
`termination_reason: superseded_by_decision` で終了する。

## 次アクション決定スクリプト（Next Action Script）

現在の LOOP_STATE から次のアクションを計算するには `decide_next_loop_action.py` を使う。
`preflight` と `investigation`、および `review`（rewrite 前）の間は orchestrator が
`decide_next_loop_action.py` を呼ばないこと（上記の「フロー上の位置」表を参照）。
#1873 で `--phase-state-file` オプションと ISSUE_REFINEMENT_PHASE_STATE_V1 phase-gate
は撤去された — タイミング制御は orchestrator（SKILL.md の Step 順序）の責務である。

**Registry id（レジストリID）**: `decide.run` (ISSUE_REFINEMENT_COMMAND_REGISTRY_V1)

```json
{"id":"decide.run","argv":["uv","run","python3",".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py","--loop-state-file","<path>","--review-result-verdict","<verdict>","--max-iterations","<N>"],"shell":false,"cwd_policy":"repo_root"}
```

Exit codes:
- `0`: pass — `NEXT_ACTION` は実行可能
- `1`: warn — `NEXT_ACTION` は実行可能だが notes あり
- `2`: human_escalation — 停止して報告
- `3`: inconsistent_state — state file が壊れているか矛盾している

優先順位: `inconsistent_state (3)` > `human_escalation (2)` > `warn (1)` > `pass (0)`。

**TODO（follow-up scope）**: `compact_review_result.py` の compact stdout は
`REVIEWED_BODY_SHA256`（reviewer が実際にレビューした live Issue body の sha256、
`ISSUE_REVIEW_RESULT_COMPACT_V1` の 9 番目のフィールド）を出力するようになった
（#1873）。`decide_next_loop_action.py` は現時点でこの値を stale reviewed-body
検出には使っていない — 大掛かりな新しい state machine を追加せず、既存の
`--loop-state-file` / `last_verdict` 比較ロジックに軽く配線する形で、将来の
follow-up Issue として拾うこと。

## scope_signal_guard_decision_v2（#1090 サイドカー）

`scope_signal_guard_decision_v2`（#1090, opt-in。`references/scope-signal-guard.md`
参照）は `LOOP_STATE_V1` 本体には含めない。`decide_next_loop_action.py` に
`--scope-signal-guard-decision-v2-file` / `--scope-signal-guard-decision-v2-json`
として別引数で直接渡す（#1873: `build_loop_state.py` の envelope pass-through 経由では
なく、orchestrator が `plan_refinement_loop.py` の出力からそのまま抽出して渡す）。

`LOOP_STATE_V1.scope_signal_guard`（`triggered` / `excluded_by_anchor_reframe` / `reason_code`）の
既存 3 フィールドの意味・値はこのサイドカーの有無に関わらず変更しない。

## scope_delta_decision.rewrite_route（#2048 サイドカー）

`REFINEMENT_LOOP_PLAN_V1.decisions.scope_delta_decision` は `additionalProperties: true`
（`schemas/refinement_loop_plan_v1.json`）であり、`LOOP_STATE_V1` 本体のフィールド集合を
拡張しない opt-in サイドカーとして `rewrite_route` を持つことがある。

`known_context.scope_delta_decision.operations`（orchestrator が
`derive_contract_patch_operations()` で導出した `CONTRACT_PATCH_PLAN_V1.operations[]`）が
planner 入力に渡されたときのみ計算される。`plan_refinement_loop.py` が
`decide_rewrite_route.decide_scope_reframe_contract_route()` を呼び出し、結果を
`scope_delta_decision.rewrite_route` として echo する:

| field | meaning |
|---|---|
| `route` | `contract_update`（通常）または `issue_editor_required`（承認済み scope reframe + empty operations[]） |
| `reason_code` | `issue_editor_required` の場合 `approved_scope_requires_full_contract_rewrite` |
| `empty_operations_fingerprint` | anchor comment URL + issue body SHA256 から導出される安定 fingerprint |
| `no_progress_retry_suppressed` | 同一 fingerprint の再実行で no-progress `contract_update` を再発行しないための signal |
| `duplicate_comment` / `should_post_comment` | 同一 fingerprint の scope-reframe comment を重複投稿しないための signal |
| `rewrite_router_implementation_allowed` | （任意）`known_context.rewrite_readback_status` が渡された場合のみ、`route_after_rewrite.compute_implementation_allowed()` の結果を collision を避けて別名で echo したもの。`scope_signal_delta.SCOPE_DELTA_AUTHORITY_V1` の `route.implementation_allowed`（trusted anchor directive 自体の implementation 許可）とは別概念であり、混同しないこと。 |

同じ判定は `scope_signal_delta.run_trusted_anchor_iteration_zero()`（実際に mutation を試みる
production 経路）でも `result["rewrite_route"]` として付与され、両者は同じ
`decide_scope_reframe_contract_route()` を呼び出すので一致する。
