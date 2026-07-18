---
name: issue-refinement-loop
description: >-
  Issue 本文の品質を調査・レビュー・改善ライトで反復改善するオーケストレーター。
  `plan_refinement_loop.py` が生成する `REFINEMENT_LOOP_PLAN_V1` を consume し、
  判定ロジックの再実装は行わない。「Issue ◯◯ を改善して」「refinement loop」で使う。
---

# Issue Refinement Loop

<!-- ISSUE_REFINEMENT_LOOP_THIN_ENTRYPOINT_V1
planner_ssot: REFINEMENT_LOOP_PLAN_V1
max_skill_lines: 500
no_prose_rejudgment: true
subagent_contract_mode: link_only
note_ja: 本ファイルは thin entrypoint 契約のメタデータであり、判定ロジックの再実装は行わない
-->

`issue-refinement-loop` は control-plane 専用の thin entrypoint である。詳細 procedure は `references/` を必要時だけ読む progressive disclosure とし、planner / reviewer / worker の判定ロジックをこのファイルへ再実装しない。

## 入力 (Inputs)

- `issue_number`（必須）: 改善対象の Issue 番号
- `max_iterations`（任意、既定 3）: review cycle の上限
- `anchor_comment_url`（任意）: 人間 Decision や差し戻しコメントを snapshot 固定して扱う対象コメント URL

## ループ方針 (Loop Policy)

```yaml
loop_policy:
  default_max_iterations: 3
  loop_iteration_approval_gate:
    default_required: false
    scope: repo_loop_iteration_only
    does_not_control:
      - Claude Code permissions.defaultMode
      - bypassPermissions
      - --dangerously-skip-permissions
      - --allow-dangerously-skip-permissions
      - --permission-mode
      - hooks PermissionRequest auto-approval
```

### loop_iteration_approval_gate

`loop_iteration_approval_gate.default_required: false`

ループの自動継続は「このリポジトリの loop policy 上の承認確認（過去に `--no-approval` と呼んでいた運用フラグ/指示）」であり、Claude Code の `--permission-mode`、`--dangerously-skip-permissions`、`permissions.defaultMode` は変更しない。loop policy は「何回まで自動で回すか」を制御し、Claude Code の permission mode は「ツール呼び出しの承認方式」を制御する。両者は直交する概念であり、loop policy の継続判断に permission mode を参照しない。

needs-fix を受け取ったとき:
- `iteration + 1 < max_iterations` → 自動継続（条件なし）
- `iteration + 1 >= max_iterations` → `human_escalation` で停止し、全 iteration 分の blocker summary を添付

## ループ構造 (Loop Structure)

```text
[Step 0: Preconditions / planner input assembly]
        ↓
[Step 0f: plan_refinement_loop.py]  → REFINEMENT_LOOP_PLAN_V1
        ↓
[Step 1: Investigation]      → codebase-investigator
[Step 1b: Web research]      → web-researcher (conditional)
        ↓
[Step 2: Review]             → issue-reviewer → REVIEW_ISSUE_RESULT_V1
        ↓
 approve → Step 4.5 → Step 5
 needs-fix → [Step 2a: Replay Arbitration]
   ├─ deterministic_fail_confirmed:
   │    iteration += 1 → Step 4
   ├─ reviewer_claim_unbacked_by_deterministic_checker:
   │    iteration を消費せず Step 2 に戻る
   ├─ reviewer_false_positive_suspected:
   │    → Step 5 (human_escalation)
   └─ input_or_runtime_error:
        → Step 5 (human_judgment_required)
```

Step 3（adversarial review）と Step 1.5（spec document review）は採用しない。Step 番号は履歴互換のため維持する。

## LOOP_STATE

ループ状態の機械可読スキーマは `schemas/loop_state.schema.json` を参照する。
フィールド定義・routing semantics・next action 決定手順は `references/loop-state.md` を参照する。
next action の決定は `scripts/decide_next_loop_action.py` に委譲する（呼び出し手順は `references/loop-state.md` を参照）。

LOOP_STATE_V1 の構築は `scripts/build_loop_state.py` を使用する。手書き JSON 渡しは禁止。
`build_loop_state.py` は `REFINEMENT_LOOP_PLAN_V1` と `ISSUE_REVIEW_RESULT_COMPACT_V1` を
入力として受け取り、スキーマ検証済みの LOOP_STATE_V1 を生成する。
詳細な builder-first フローは `references/loop-state.md` の「Building LOOP_STATE_V1」セクションを参照する。

routing-critical フィールド（`scope_rollup_decision`、`scope_signal_guard`、`delivery_rollup`、
`follow_up_materialization`、`superseded_decision`）の定義は `references/loop-state.md` が SSOT。
orchestrator はこれらのフィールドを直接 prose 再判定しない。

主要な consumer フィールドの例: `web_research:` (web-researcher 実行状態)。
web_research 結果に含まれる `critical_claims` の未解決 claim は human_escalation へ倒す。

## 手順 (Procedure)

### Step 0: 前提条件 (Preconditions)

1. Issue 本文と必要コメントを取得し、`state/needs-human` / `state/done` の hard stop を確認する。
2. `anchor_comment_url` がある場合は snapshot を固定し、対象 Issue 所属を検証する。
3. scope rollup preflight を mutation-free で実行し、`LOOP_STATE.scope_rollup_decision` を記録する。
4. Product/Spec routing signal を検知し、`LOOP_STATE.product_spec_context` を更新する。
5. 本 Issue への refinement 継続が確定した後に、stale な `state/blocked` / `state/queued` を hygiene として除去する。

### Step 0f: Planner 結果の消費 (Planner Consumption)

`run_refinement_preflight.py` wrapper を実行して Issue fetch・anchor comment 構造検証・planner stdin 組立・`REFINEMENT_LOOP_PLAN_V1` 生成を一括で実行する。wrapper は `plan_refinement_loop.py` を SSOT として呼び出す薄い adapter であり、判断ロジックは planner に委譲する。

コマンドの canonical な argv 定義は `ISSUE_REFINEMENT_COMMAND_REGISTRY_V1`（`scripts/command_registry.py`）に集約されている。SubAgent / main thread は手書き shell string を消費せず、registry entry（`preflight.run` 等）を参照する。

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py \
  --issue-number <N> \
  --repo <owner/repo> \
  [--anchor-comment-url <URL>]
```

root checkout（canonical main root / default branch）から anchor comment を指定して preflight を実行する場合は、上記の直接 wrapper 呼び出しではなく、`preflight.run.with_anchor`（`preflight.run` の sibling exact profile、Issue #1498）を正規の privileged executor 経由で実行する。以下は exact command policy が要求する厳密な token 列（`--locked` を含まない）そのものであり、`uv run --locked` governance policy の対象ではない:

<!-- policy-example --><!-- 以下は方針の例を示すコメントであり、実行対象のコマンド構文には影響しない -->
```bash
uv run python3 scripts/agent-guards/skill_runtime_exec.py \
  --command-id preflight.run.with_anchor \
  --issue-number <N> \
  --repo <owner/repo> \
  --anchor-comment-url <canonical GitHub issue comment URL>
```

`--anchor-comment-url` は `https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<M>` の canonical shape のみを受け付け、`--issue-number` / `--repo` と URL 内の owner/repo/issue 番号が一致しない場合は拒否される（context-binding）。`preflight.run` 自体の argv / placeholders / execution_class はこの sibling profile の追加によって一切変更されない。

wrapper の出力フィールドを確認する:

**canonical stdout フィールド（機械可読）:**
- `STATUS: pass | warn | blocked | environment_failure` — 常に出力される
- `NEXT_ACTION: proceed | proceed_with_notes | human_judgment_required | fix_environment` — 常に出力される
- `MUST_READ:` — 読むべきパス一覧（空の場合は省略）
- `COMMANDS_JSON:` — full command spec objects（canonical machine-consumable、空の場合は省略）
- `COMMANDS_DISPLAY:` — human-readable display（display_only=true、空の場合は省略）
- `BLOCKERS:` — ブロッカーコード一覧（空の場合は省略）
- `ARTIFACT:` — 書き込まれた artifact の key: path 一覧（空の場合は省略）

**非 canonical / 抑制フィールド:**
- `SUMMARY` — 人間向け prose、オーケストレーターは consume しない
- `DO_NOT_READ` — 予約済み（現在は常に空）、consumers は欠如に依存してはならない
- `EVIDENCE` — raw issue body / comments は stdout に出力されない（artifact のみ）

**warn (exit 1) の定義:**
planner exit 0 かつ `fail_closed.required == false` かつ `decisions.*.confidence` に `"unknown"` が 1 つ以上含まれる場合、`STATUS: warn` / exit 1 を返す。human note が必要だが blocking ではない。`NEXT_ACTION: proceed_with_notes` に従って継続できる。

- `NEXT_ACTION:` に従って後続ステップを決定する
- `ARTIFACT:` の `refinement_preflight_result_v1` パスから `fail_closed` / `decisions` を参照する
- `ARTIFACT:` の `planner_input` パスで planner へ渡した stdin JSON を確認できる

`STATUS: blocked` または `STATUS: environment_failure` の場合は停止し、人間判断へ送る。`investigation_policy` / `web_research_policy` / `scope_signal_guard` / `follow_up_materialization` の判定は planner を SSOT とし、このファイルで prose 再判定しない。

**Phase gate**: preflight 完了後に `ISSUE_REFINEMENT_PHASE_STATE_V1` を生成する。
`scope_signal_guard.triggered: true` が含まれる場合でも、preflight phase では `hard_stop_eligible: false`
であるため、`decide_next_loop_action.py` を呼ばない。planner の
`investigation_policy` / `web_research_policy` に従って Step 1 / Step 1b / Step 2 へ進む。

```bash
# Phase state 生成（Step 0f 完了後）
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/build_refinement_phase_state.py \
  --phase preflight \
  --source-kind refinement_preflight_result_v1 \
  --source-path <refinement_preflight_result_v1 path> \
  --output-path <phase_state_output_path>
```

参照:

- `references/refinement-loop-plan-output.md`
- `references/scope-signal-guard.md`

#### Parent-owned preflight（isolation worktree agent への委譲時）

`preflight.run`（`skill_runtime_command_policy.py` が要求する `required_cwd: canonical_main_root` / `required_branch: default_branch` invariant）と、Agent tool の `isolation: "worktree"` で生成される汎用 worktree の cwd は衝突する。この衝突を解決するため、isolation worktree agent へ Step 0f 相当の作業を委譲する場合は **parent-owned preflight** 方針を採用する: parent（orchestrator 自身、canonical main root・default branch で稼働する 主スレッド）が `preflight.run` を実行し、bounded な結果（`NEXT_ACTION` / `MUST_READ` / `ARTIFACT` 等の canonical stdout フィールドのみ）を isolation agent へ渡す。isolation agent はその bounded な結果を入力として consume するだけであり、preflight 実行そのものは行わない。

isolation agent は `skill_runtime_exec.py`（exact executor）を自ら実行しない。isolation agent は `run_refinement_preflight.py`（direct wrapper）も自ら実行しない。いずれも parent が canonical main root で実行し、その出力のみを isolation agent へ引き渡す。

`agent-*` 等の未保証な isolation worktree 命名パターンを `preflight.run` の認可根拠として追加しない。`skill_runtime_command_policy.py` の `required_cwd` / `required_branch` invariant は緩和せず、認可判定は canonical main root / default branch の実行コンテキストのみを根拠とする。

既存の Step 0f 直接実行 bash block（`run_refinement_preflight.py` を直接呼ぶ例）は、orchestrator 自身が canonical main root もしくは canonically-named issue worktree から直接実行する場合に限定される。isolation worktree agent からは直接実行しない — isolation agent への委譲時は必ず上記の parent-owned preflight 方針に従い、parent が実行した結果のみを渡す。

### Step 1: 事前調査 (Investigation)

`REFINEMENT_LOOP_PLAN_V1.decisions.investigation_policy.required == true` の場合のみ `codebase-investigator` を起動する。返却される構造化結果を受け取り、`final_classification` の確定責務は main thread が保持する。SubAgent は mutation してはいけない。

anchor comment の fact-check 契約、`ANCHOR_COMMENT_CONTEXT_V1`、`ANCHOR_COMMENT_FACT_CHECK_RESULT_V1`、`REPO_EVIDENCE_REF_V1` の扱いは `references/anchor-comment-handling.md` を参照する。

### Step 1b: 外部Web調査 (Web Research)

`REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy.required == true` の場合のみ `web-researcher` を起動する。orchestrator は `WEB_RESEARCH_RESULT_V1` を opaque に扱い、consumer field だけを `LOOP_STATE.web_research` へ反映する。retry / fallback / raw grounding state は保持しない。

Step 1（codebase-investigator）と Step 1b（web-researcher）は、両方が required の場合に並列実行できる。ただし両 SubAgent の結果を Step 2 前に合流させること。

web-researcher が critical claim にエビデンスを示せず、ハルシネーション疑いと判定した場合は `human_escalation` に倒す（Step 5）。

詳細は `references/web-research-routing.md` を参照する。

### Step 2: レビュー (Review)

`issue-reviewer` SubAgent が `review-issue` を実行し、`ISSUE_REVIEW_RESULT_COMPACT_V1` を返す。orchestrator は reviewer の prose を再判定せず、artifact と deterministic checker を使う arbitration step を `needs-fix` と Step 4 の間に挟む。

消費側契約 (consumer contract): `ISSUE_REVIEW_RESULT_COMPACT_V1`（正本 (SSOT): `.claude/skills/issue-refinement-loop/scripts/compact_review_result.py`）

**validator-first 順序（Issue #1507 AC23、routing table より前に評価する）**: orchestrator は approve / needs-fix いずれの経路でも、SubAgent stdout の生フィールドを consume する前に、必ず `validate_review_compact_output.py`（`review_compact.validate`, command_registry.py 登録済み、`--issue-number` 必須引数）へ SubAgent の最終応答テキストをそのまま（re-transcribe せず）渡し、`REVIEW_COMPACT_VALIDATION_RESULT_V1` を得る。**validator 完了前に `VERDICT` / `NEXT_ACTION` / `ARTIFACT` / `REVIEWER_BLOCKER_CLAIM` を読んではならない。** `validation_status != valid` の場合は routing を `human_judgment_required` に固定する（fail-closed）。validation が `valid` の場合のみ、`normalized_payload` を根拠に以下の routing table を評価する:

- `VERDICT: approve` → Step 4.5 へ
- `VERDICT: needs-fix` → Step 2a（parent-local replay integrity binding、`parent_replay_binding.py`）を実行し、orchestrator 自身が計算した `PARENT_REPLAY_VERDICT` / `PARENT_REPLAY_ROUTING` / `PARENT_REPLAY_SHOULD_CONSUME` / `PARENT_REPLAY_BODY_SHA256` の結果のみで Step 4 / Step 2 / human escalation を分岐する（Issue #1532。子 SubAgent が返す `REVIEWER_BLOCKER_CLAIM` は bounded な untrusted claim であり、そのまま routing に使ってはならない。orchestrator は子 worktree の raw `compact_review_result_v1` artifact パスを別途 open/read しない — Issue #1472）
- full structured data は `EVIDENCE:` / `ARTIFACT:` パスから取得する（main context には返らない、validator 通過後のみ参照可）

anchor comment により stale approval を無効化する場合も、raw snapshot は Step 4 に渡さず、正規化済み `anchor_comment_feedback` だけを渡す。

review 後、phase state を `review` フェーズに更新してから verdict に応じて routing する。

**重要**: `review` phase は pre-rewrite phase であるため `decide_next_loop_action.py` を呼んではならない。
`review` phase の `allowed_routers` に `decide_next_loop_action.py` は含まれない（B2 Router Rule）。
`review` phase での routing は VERDICT に基づいて直接行う:

- `VERDICT: approve` → phase state を `decide_next_action` に更新してから Step 4.5 へ
- `VERDICT: needs-fix` → phase state を `rewrite` に更新し、直後に Step 2a replay arbitration を実行してから Step 4 / Step 5 を決める

phase state の更新（Issue #1507 AC24: `--review-validation-result-path` は上記 validator の出力先を指し、`validation_status: valid` でない場合は非ゼロ終了し phase-state を生成しない構造的ゲート。`--phase review` かつ `--source-kind issue_review_result_compact_v1` の組み合わせでのみ必須）:

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/build_refinement_phase_state.py \
  --phase review \
  --source-kind issue_review_result_compact_v1 \
  --source-path <review_result_path> \
  --review-validation-result-path <review_compact_validation_result_v1 path> \
  --output-path <phase_state_output_path>
```

`decide_next_loop_action.py` は `post_rewrite_check` または `decide_next_action` phase でのみ呼ぶ:

```bash
# post_rewrite_check または decide_next_action phase のみ
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py \
  --loop-state-file <loop_state_path> \
  --review-result-verdict <approve|needs-fix> \
  --phase-state-file <phase_state_output_path>
```

`review` phase では `hard_stop_eligible: false`（pre-rewrite phase）のため、
`scope_signal_guard.triggered: true` があっても `decide_next_loop_action.py` を呼ばない。
hard stop 判定は `post_rewrite_check` / `decide_next_action` phase（`hard_stop_eligible: true`）で行う（AC4 / #919 回帰維持）。

#### Step 2a: 親ローカル Replay 整合性束縛（Parent-Local Replay Integrity Binding、Issue #1532）

`VERDICT: needs-fix` の直後に、reviewer blocker が deterministic checker に裏付けられているかを **orchestrator（parent）自身**が `parent_replay_binding.py` で独立に確認する。`issue-reviewer` は `reviewer_claim_replay.py` を実行せず、bounded な `REVIEWER_BLOCKER_CLAIM`（`REVIEWER_BLOCKER_CLAIM_V1`: `body_sha256` + `blockers[].{reviewer_blocker_code,message,line_start,line_end}` のみ）を stdout に返すだけである（Blocker 1/2。V1 の child self-report `REPLAY_VERDICT` 等は producer 契約から廃止された）。

これは **parent-local replay integrity binding** であり、child SubAgent の producer identity・署名・鍵管理・supply-chain provenance を証明する attestation ではない（Safety Claim Matrix 対象外）。

orchestrator は `readiness_result` / `vc_syntax_result` / `vc_preflight_result` / 現在の Issue body raw bytes snapshot / `previous_state`（`reviewer_claim_replay_state_store.py --read`）/ identity（`repository_full_name`/`issue_number`/`refinement_session_id`/`iteration_id`）を自ら取得・保存・readback し（child の raw artifact や `findings`/`checker_evidence`/`deterministic_checks` は一切使用しない）、`parent_replay_binding.py`（`--reviewer-blocker-claim-file` / `--readiness-result-file` / `--current-body-file` / identity 引数）へ渡して `PARENT_REPLAY_BINDING_ARTIFACT_V1` を得る。child claim は `additionalProperties: false` schema で fail-closed 検証され、`deterministic_backed` は parent 自身の evidence のみを根拠とする。`--iteration-id` を渡すため wall-clock 値は生成されない（High-2）。CLI の exact 引数と例は `docs/dev/workflows/issue-refinement-loop-design.md` の「Step 2a」を参照。

consecutive-unbacked state は orchestrator が所有する（`reviewer_claim_replay_state_store.py`、#1515）。

orchestrator は child の raw stdout bytes を、独立コマンド `review_compact.validate_intermediate_v1`（`emit_parent_review_envelope_v2.py --validate-intermediate`、Issue #1541 PR #1557 OWNER REQUEST_CHANGES Blocker 1）へ渡し、strict validation 済みの `envelope_kind`（`approve` / `needs_fix_intermediate`）・`normalized_payload`・`canonical_reviewer_blocker_claim` を得る。手動 `startswith()`/`json.loads()` によるフィールド抽出は禁止する。`needs_fix_intermediate` の場合のみ `canonical_reviewer_blocker_claim` をファイル化し `parent_replay.bind` へ渡す。得られた claim envelope と上記 binding artifact を `emit_parent_review_envelope_v2.py` へ渡し、決定論的に `ISSUE_REVIEW_RESULT_COMPACT_V2`（15 行）を得る -- approve は `review_compact.emit_approve`（binding/body/session/iteration の引数を一切持たず、それらのファイルを開かない）、needs-fix は `review_compact.emit_v2`（Blocker 2 で binding/body を開く前に bounded intermediate を strict 分類する順序に修正済み）を使う。この producer は `PARENT_REPLAY_*` の 6 行を binding artifact からのみ導出し、child claim の digest・binding artifact 自身の digest 自己整合性・identity（repository/issue/session/iteration/body）を独立に照合してから envelope を組み立てるため、旧来の「orchestrator が f-string で 6 行を追記する」手動 assembly（テスト専用 `_assemble_v2_envelope()` 相当）は production 経路から廃止された。得られた envelope は `validate_review_compact_output.py --v2`（`review_compact.validate_v2`、`--binding-artifact-file` 等すべて必須、High-1）で binding artifact の strict schema・digest 再計算・identity/body 照合・envelope 全フィールドの exact 照合を経てから consume する。`PARENT_REPLAY_VERDICT` は `reviewer_claim_replay.py` の `_LEGACY_VERDICT_MAP_V1` と同期した5値 enum。

`validation_status: valid` の場合のみ `reviewer_claim_replay_state_store.py --write-v2`（`state.write-v2`、`--expected-parent-binding-digest` 必須）で `PARENT_REPLAY_NEXT_STATE` を永続化する。state store 自身が `schema`/`schema_version`/`envelope_kind`/`violations == []`/identity を再検証するため、caller が組み立てた偽装 payload では state が更新されない（High-3）。詳細フローは `references/loop-state.md` の「REVIEWER_CLAIM_REPLAY_STATE_V2」を参照。

出力契約（`PARENT_REPLAY_VERDICT` の consume ルーティング）:
- `deterministic_fail_confirmed` → Step 4 rewrite。`PARENT_REPLAY_SHOULD_CONSUME: true`
- `checker_artifact_inconsistency` → `fix_checker_artifact` に従い checker artifact を修正後 Step 2 に戻す（iteration 消費なし）
- `reviewer_claim_unbacked_by_deterministic_checker` → non-blocking downgrade、iteration 消費なしで Step 2 に戻す
- `reviewer_false_positive_suspected` → 同一 body/lane で 2 回連続 unbacked。`human_escalation` で停止
- `input_or_runtime_error` → `human_judgment_required` で停止

### Step 4: 書き換え (Rewrite)

`issue-author` SubAgent に opaque forwarding payload を渡して本文を更新する。AC/VC の baseline fail expectation と review 時の扱いを取り違えないこと。詳細な reflection guard は `references/ac-vc-reflection.md` を参照する。

issue-author 起動前に、現在本文に対して pre-author static readiness check を実行する。

```bash
uv run --locked python3 .claude/skills/issue-contract-review/scripts/contract_readiness_check.py \
  --mode preflight-static \
  --body-file <current_body_file>
```

実行コマンド例 (inline form): `contract_readiness_check.py --mode preflight-static --body-file <current_body_file>`

生成側契約 (producer contract): `READINESS_FORWARDING_PAYLOAD_V1`

```yaml
READINESS_FORWARDING_PAYLOAD_V1:
  readiness_result:
    status: go | needs_fix | human_judgment | input_or_runtime_error
    body_sha256: <sha256>
    source_checks:
      - contract_readiness_check.py --mode preflight-static
    errors: []
    readiness_result_ref: <artifact-or-path>
```

`preflight-static` は static body/readiness の事前確認専用であり、execute-mode の `unexpected_pass` 検出は扱わない。

readiness 結果に応じた分岐処理 (readiness routing):

```yaml
exit_code_0:
  status: go
  action: invoke_issue_author
  readiness_errors: []
exit_code_1:
  status: needs_fix
  action: invoke_issue_author_with_readiness_result
exit_code_2:
  status: human_judgment
  action: skip_issue_author_and_go_step5
exit_code_3:
  status: input_or_runtime_error
  action: human_escalation
```

消費側契約 (consumer contract): `ISSUE_AUTHOR_RESULT_COMPACT_V1`（正本 (SSOT): `.claude/skills/issue-refinement-loop/scripts/compact_author_result.py`）

- `STATUS: ok` / `BODY_HASH: <sha256>` → 更新成功、`NEXT_ACTION: proceed` で Step 2 に戻る
- `STATUS: no_change` → 変更なし、`NEXT_ACTION: proceed` で Step 2 に戻る
- `STATUS: failed` → 修正失敗、`NEXT_ACTION: human_judgment_required`、Step 5 human_escalation へ
- `partial_failure` は廃止。issue-author は `ok` / `no_change` / `failed` の 3 値のみを返す。
- full mutation result は `ARTIFACT:` パスから取得する（main context には返らない）

rewrite ループの反復ごとに、checker 実行後に `scripts/decide_rewrite_route.py` を呼び出して `max_rewrite_attempts` 超過・body hash 変化なし・missing set 単調減少なしを runtime で強制し、`route`（`continue_rewrite` / `proceed_to_review` / `human_judgment_required`）に従って routing する。invocation 手順・state 永続化・`human_judgment_required` 連動は `references/termination-policy.md` の「Rewrite Loop Runtime Router（#664）」セクションを SSOT とする。orchestrator は attempt 数や no-progress を prose で再判定しない。

### Step 4.5: 子Issue/follow-up の実体化 (Materialization)

delivery-rollup parent の child materialization gate と、approve 後の follow-up 起票候補は `references/follow-up-materialization.md` を参照する。dedupe は title ではなく `dedupe_key` で行う。

### Step 5: 終了処理 (Termination)

終了条件、`human_escalation` 経路、scope change signal 停止、loop termination table は `references/termination-policy.md` を参照する。

`approved` 終了時は `LOOP_HANDOFF_RESULT_V1` marker を終了コメントに出力する（形式・routing rules は `references/termination-policy.md#LOOP_HANDOFF_RESULT_V1` 参照）。出力は `<!-- LOOP_HANDOFF_RESULT_V1 -->` HTML comment と fenced YAML block の 2 要素。

#### scope_signal_guard 停止時の termination_cause 正規化手順

`scope_signal_guard.triggered=true` かつ `excluded_by_anchor_reframe=false` のとき、orchestrator は以下の手順で termination payload を組み立てる:

1. `decide_next_loop_action.py` の出力から `TERMINATION_CAUSE:` 行を読み取る（`human_judgment_required` が出力される）
2. `termination_cause: human_judgment_required` を termination payload に設定する
3. `BLOCKERS:` 行の値（`scope_signal_guard_triggered`、`scope_signal_guard_reason_code:<code>` 等）を `blockers_summary` に転記する
4. `publish_termination_report.py` に渡す

`scope_signal_guard.reason_code` を `termination_cause` に直接渡してはならない。`VALID_TERMINATION_CAUSES` に含まれない diagnostic code は renderer が reject する（#919 回帰防止）。

詳細は `references/termination-policy.md` の「scope_signal_guard 停止時の termination payload 正規化」セクションを参照する。

## 終了レポート投稿フロー (Termination Report Publish Flow)

終了レポートの GitHub 投稿は `publish_termination_report.py` を経由して行う。

```bash
# TERMINATION_REPORT_INPUT_V1 JSON を stdin から渡す
echo '{"termination_reason":"approved","issue_number":42}' | \
  uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py \
    --issue-number 42
```

`human_escalation` の publish では、`termination_cause` omitted / `null` は `human_judgment_required` へ正規化され、`Cause: none` を出さない。caller が明示した valid cause は保持される。canonical key は `blockers_summary`。`blocker_summary` は旧 alias として validation 前に `blockers_summary` へ正規化するが、alias conflict や alias 側の型不正は fail-closed になる。

human_escalation の入力例（termination_cause と blockers_summary を明示）:

```bash
echo '{
  "termination_reason": "human_escalation",
  "termination_cause": "human_judgment_required",
  "issue_number": 829,
  "iteration": 3,
  "blockers_summary": [
    "オーナー判断が必要",
    "スコープの矛盾が未解決"
  ]
}' | uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py \
  --issue-number 829
```

`publish_termination_report.py` は以下の責務を持つ:

1. `render_termination_report.py` を `subprocess.run([sys.executable, ...], shell=False, ...)` で呼び出す
2. stdout JSON の `schema` / `schema_version` / `publishable` / `body` / `reason_code` を検証する
3. `publishable=true` かつ `body` が非空文字列の場合のみ `gh issue comment --body-file` を呼ぶ
4. `publishable=false`、renderer 異常、validation 失敗の場合は gh を呼ばず fail-closed で終了し、reason_code / timestamp をローカル artifact に記録する

詳細な publisher 仕様は `.claude/skills/issue-refinement-loop/scripts/publish_termination_report.py` を参照する。

## 参照マップ (Reference Map)

| topic | primary reference |
|---|---|
| loop state schema | `schemas/loop_state.schema.json` |
| loop state field definitions | `references/loop-state.md` |
| anchor comment handling | `references/anchor-comment-handling.md` |
| scope signal guard | `references/scope-signal-guard.md` |
| AC/VC reflection | `references/ac-vc-reflection.md` |
| follow-up materialization | `references/follow-up-materialization.md` |
| web research routing | `references/web-research-routing.md` |
| termination policy | `references/termination-policy.md` |
| planner output contract | `references/refinement-loop-plan-output.md` |
| scope rollup preflight | `references/scope-rollup-policy.md` |
| command registry | `scripts/command_registry.py` — `ISSUE_REFINEMENT_COMMAND_REGISTRY_V1` |

## 安全策 (Guardrails)

- thin entrypoint を維持し、判定ロジックは planner / reviewer / worker の SSOT を consume するだけに留める
- control-plane のみを担当し、Issue/PR mutation や final judgment の一部を SubAgent に委譲しすぎない
- raw anchor comment snapshot を reviewer feedback や title rewrite 入力へ直接流さない
- `WEB_RESEARCH_RESULT_V1` の retry/fallback/attempt log は link-only とし、`#394` の責務へ越境しない
- `max_iterations` 超過時は fail-close する

## スコープ変更時の停止条件 (Scope Change Stop Conditions)

iteration 中に以下が新規追加された場合は `termination_reason: human_escalation` で停止する。

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に新規ディレクトリや別アーキテクチャ層が追加された
- `## Acceptance Criteria` に新規の低検証可能 AC が追加された

詳細な signal semantics は `references/scope-signal-guard.md` を参照する。

## Out of Scope

- planner script の判定ロジック追加や schema 変更
- `web-researcher` / `gemini-cli-headless-delegation` の retry / fallback / attempt log 設計変更
- `.claude/agents/*.md` の責務移動
- Agent SDK 化

## Verification Commands

```bash
# AC2 / AC5
rg -n "ISSUE_REFINEMENT_LOOP_THIN_ENTRYPOINT_V1|REFINEMENT_LOOP_PLAN_V1|plan_refinement_loop.py|schema validation|fail_closed" .claude/skills/issue-refinement-loop/SKILL.md

# AC3
test "$(wc -l < .claude/skills/issue-refinement-loop/SKILL.md)" -le 500

# AC4 / AC6 / AC10
rg -n "references/anchor-comment-handling\.md|references/web-research-routing\.md|references/follow-up-materialization\.md|references/termination-policy\.md|references/ac-vc-reflection\.md|references/scope-signal-guard\.md" .claude/skills/issue-refinement-loop/SKILL.md
test -f .claude/skills/issue-refinement-loop/references/index.md
test -f .claude/skills/issue-refinement-loop/references/anchor-comment-handling.md
test -f .claude/skills/issue-refinement-loop/references/web-research-routing.md
test -f .claude/skills/issue-refinement-loop/references/follow-up-materialization.md
test -f .claude/skills/issue-refinement-loop/references/termination-policy.md
test -f .claude/skills/issue-refinement-loop/references/ac-vc-reflection.md
test -f .claude/skills/issue-refinement-loop/references/scope-signal-guard.md
rg -nq "\| topic \| file \| loaded_when \| owner \| moved_from \| must_not \|" .claude/skills/issue-refinement-loop/references/index.md

# AC8 / AC9
test -f .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py
uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py -v

# AC7
pnpm typecheck
pnpm lint
pnpm test
pnpm build
uv run --locked pytest .claude/skills/issue-refinement-loop/tests/ -v

# Issue #1507 AC11 / AC23
rg -n "validate_review_compact_output" .claude/skills/issue-refinement-loop/SKILL.md
rg -n "checker_artifact_inconsistency" .claude/skills/issue-refinement-loop/SKILL.md
rg -n "validator 完了前に" .claude/skills/issue-refinement-loop/SKILL.md
```

## 関連資料 (Related)

- `.claude/skills/review-issue/SKILL.md` — レビュー手順の正本
- `.claude/skills/edit-issue/SKILL.md` — 本文編集の正本
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md` — 外部調査委譲の正本
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター境界の設計原則
- `docs/dev/workflow.md` — 開発フロー全体の正本
- `docs/dev/agent-run-report.md` — run report finalize / posting handoff 規約

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドを削らず、人間向け説明・証跡の再掲のみを削減する。
