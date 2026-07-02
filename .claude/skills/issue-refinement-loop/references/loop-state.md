---
topic: loop_state
file: references/loop-state.md
loaded_when: need to understand LOOP_STATE field semantics or routing decisions
owner: issue-refinement-loop orchestrator
moved_from: SKILL.md##LOOP_STATE Summary
must_not: re-implement routing logic — use decide_next_loop_action.py
schema: schemas/loop_state.schema.json
note_ja: このファイルは LOOP_STATE_V1 スキーマのフィールド定義とルーティング意味論を日本語で解説する。
---

# LOOP_STATE リファレンス

`LOOP_STATE_V1` スキーマの全フィールド定義とルーティング意味論。
正本となる機械可読スキーマは `schemas/loop_state.schema.json` である。

## LOOP_STATE_V1 の構築

`LOOP_STATE_V1` を planner と review の結果から構築するには `build_loop_state.py` を使う。
**LOOP_STATE の JSON を手書きしてはならない** — スキーマ検証と provenance を保証するため builder を使うこと。

```bash
uv run python3 .claude/skills/issue-refinement-loop/scripts/build_loop_state.py \
  --planner-result-file <REFINEMENT_LOOP_PLAN_V1 path> \
  --review-result-file <ISSUE_REVIEW_RESULT_COMPACT_V1 path> \
  --issue-number <N> \
  --iteration <0-indexed> \
  [--max-iterations <N>] \
  [--blockers-history-file <path>] \
  [--schema-file <path>] \
  --out <output path>
```

builder は以下を含む `LOOP_STATE_BUILD_RESULT_V1`（stdout JSON）を出力する。
- `status`: `ok` | `invalid`
- `loop_state_path`: 検証済み LOOP_STATE_V1 の書き込み先
- `loop_state_sha256`: 整合性確認用のコンテンツハッシュ
- `errors[]`: スキーマ検証エラー（path, message, schema_path）
- `provenance`: planner/review 入力のハッシュとソースメタデータ

builder の制約:
- `next_action` を決定しない（それは `decide_next_loop_action.py` の役割）
- GitHub mutation（`gh` コマンド）を実行しない
- `iter_errors()` を使い `schemas/loop_state.schema.json` に対して出力を検証する（全エラーを収集）
- 入力はファイルパスのみ — 生の JSON 文字列は拒否される

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

## Builder 入力から LOOP_STATE_V1 フィールドへのマッピング

| LOOP_STATE_V1 field | Source |
|---|---|
| `issue_number` | CLI `--issue-number`（planner/review artifact と照合検証） |
| `iteration` | CLI `--iteration` |
| `max_iterations` | CLI `--max-iterations`（デフォルト 3） |
| `web_research_policy` | `REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy` |
| `scope_signal_guard` | `REFINEMENT_LOOP_PLAN_V1.decisions.scope_signal_guard` |
| `delivery_rollup` | `REFINEMENT_LOOP_PLAN_V1.decisions.delivery_rollup` |
| `follow_up_materialization` | `REFINEMENT_LOOP_PLAN_V1.decisions.follow_up_materialization` |
| `last_verdict` | `ISSUE_REVIEW_RESULT_COMPACT_V1.VERDICT` |
| `blockers_history` | CLI `--blockers-history-file` または空配列 |
| `termination_reason` | 常に `null`（builder は loop を終了させない） |

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

`scope_signal_guard.triggered` は **phase-sensitive** である。その意味は現在の
`ISSUE_REFINEMENT_PHASE_STATE_V1.scope_signal_semantics.triggered_meaning` に依存する。

| phase | triggered_meaning | hard_stop_eligible | effect |
|---|---|---|---|
| `preflight` | `continue_investigation` | false | preflight 中のシグナル → investigation/review へ進む。`decide_next_loop_action.py` を呼ばない |
| `investigation` | `continue_investigation` | false | investigation 中のシグナル → 継続。hard stop ではない |
| `review` | `continue_investigation` | false | rewrite 前 phase。`decide_next_loop_action.py` を呼ばず、VERDICT に基づき直接ルーティングする |
| `post_rewrite_check` | `hard_stop_candidate` | true | rewrite 後のシグナル → `human_escalation` |
| `decide_next_action` | `hard_stop_candidate` | true | routing phase 中のシグナル → `human_escalation` |
| `rewrite` | `ignored` | false | rewrite 中のシグナル → 無視 |
| `publish` / `terminate` | `ignored` | false | publish/terminate 中のシグナル → 無視 |

**Phase contract**: `LOOP_STATE_V1` は `phase` フィールドを持たない。phase は
`ISSUE_REFINEMENT_PHASE_STATE_V1`（`build_refinement_phase_state.py` が生成）で別途追跡される。

`triggered == true` かつ `excluded_by_anchor_reframe == false` かつ
`hard_stop_eligible == true`（つまり phase が `post_rewrite_check` または `decide_next_action`）の場合、
loop は `human_escalation` で停止する。`review` phase は明示的に hard-stop の対象外であり、
`decide_next_loop_action.py` を呼ばずに `VERDICT` に基づき直接ルーティングする。シグナルの分類と
phase-gate ルールについては `references/scope-signal-guard.md` を参照。

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
**Phase gate**: routing が許可されている phase では常に `--phase-state-file` を渡すこと。
`preflight` と `investigation` の phase では `decide_next_loop_action.py` を呼ばないこと。

**Registry id（レジストリID）**: `decide.run` (ISSUE_REFINEMENT_COMMAND_REGISTRY_V1)

```json
{"id":"decide.run","argv":["uv","run","python3",".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py","--loop-state-file","<path>","--review-result-verdict","<verdict>","--max-iterations","<N>","--phase-state-file","<phase_path>"],"shell":false,"cwd_policy":"repo_root"}
```

Exit codes:
- `0`: pass — `NEXT_ACTION` は実行可能
- `1`: warn — `NEXT_ACTION` は実行可能だが notes あり
- `2`: human_escalation — 停止して報告
- `3`: inconsistent_state — state file が壊れているか矛盾している

優先順位: `inconsistent_state (3)` > `human_escalation (2)` > `warn (1)` > `pass (0)`。

## Phase State の生成

`ISSUE_REFINEMENT_PHASE_STATE_V1` を生成するには `build_refinement_phase_state.py` を使う。

**Registry id（レジストリID）**: `phase_state.build` (ISSUE_REFINEMENT_COMMAND_REGISTRY_V1)

```json
{"id":"phase_state.build","argv":["uv","run","python3",".claude/skills/issue-refinement-loop/scripts/build_refinement_phase_state.py","--phase","<phase_name>","--source-kind","<kind>","--source-path","<artifact_path>","--output-path","<output_path>"],"shell":false,"cwd_policy":"repo_root"}
```

生成された `ISSUE_REFINEMENT_PHASE_STATE_V1` は `scope_signal_semantics.hard_stop_eligible` を含み、
これが現在の phase で `scope_signal_guard.triggered` が hard stop になるかどうかを決定する。


## scope_signal_guard_decision_v2（build_loop_state.py の envelope pass-through 拡張フィールド, #1090）

`build_loop_state.py` は `plan['scope_signal_guard_decision_v2']`（#1090, opt-in。
`references/scope-signal-guard.md` 参照）が存在する場合、それをそのまま
`LOOP_STATE_BUILD_RESULT_V1.scope_signal_guard_decision_v2` として CLI 出力 envelope に含める。

**`LOOP_STATE_V1` 本体（`loop_state.schema.json` で検証される部分）には含めない。**
`schemas/loop_state.schema.json` は本 Issue の Allowed Paths 外であり、
`additionalProperties: false` の既存スキーマを変更せずに lane 情報を surfaces する必要があるため、
`_make_build_result()` が構築する CLI envelope 側にのみ追加する（`LOOP_STATE_BUILD_RESULT_V1` は
jsonschema 検証対象外）。`build_loop_state()` 関数の戻り値は
`(loop_state, blocked_reasons, scope_signal_guard_decision_v2)` の 3-tuple になる。

`LOOP_STATE_V1.scope_signal_guard`（`triggered` / `excluded_by_anchor_reframe` / `reason_code`）の
既存 3 フィールドの意味・値は変更しない。
