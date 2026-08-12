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
- `anchor_comment_url`（任意）: snapshot 固定して扱う対象コメント URL。URL 単独から origin は推定せず、canonical command profile が human context / agent report / unlabeled を明示する。

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
[Step 2: Review]             → issue-reviewer → root_review_pipeline(V2 delegate) → ISSUE_REVIEW_RESULT_COMPACT_V2
        ↓
 VERDICT を decide_next_loop_action.py にそのまま渡す（bounded decision, #1873）
   ├─ approve:
   │    → Step 4.5 → Step 5
   ├─ needs-fix かつ iteration < max_iterations:
   │    iteration += 1 → Step 4（rewrite）
   └─ needs-fix かつ iteration >= max_iterations（または blocked）:
        → Step 5 (human_review_required)
```

Step 2a（旧 Replay Arbitration、#1532 V2 契約）は #1873 で撤去された。orchestrator は
`issue-reviewer` の VERDICT を独立に再計算せず直接信頼する。Step 3（adversarial review）
と Step 1.5（spec document review）は採用しない。Step 番号は履歴互換のため維持する。

## LOOP_STATE

`LOOP_STATE_V1` のフィールド定義・routing semantics・next action 決定手順は
`references/loop-state.md` を参照する（#1873: `schemas/loop_state.schema.json` の JSON
Schema ファイルと `build_loop_state.py` builder は撤去済み — orchestrator が planner /
review の結果から直接組み立てる plain dict である）。next action の決定は
`scripts/decide_next_loop_action.py` に委譲する（呼び出し手順は `references/loop-state.md`
を参照）。

routing-critical フィールド（`scope_rollup_decision`、`scope_signal_guard`、`delivery_rollup`、
`follow_up_materialization`、`superseded_decision`）の定義は `references/loop-state.md` が SSOT。
orchestrator はこれらのフィールドを直接 prose 再判定しない。

主要な consumer フィールドの例: `web_research:` (web-researcher 実行状態)。
未解決 external claim は、その decision dependency が `dispositive` で repository
evidence が disposition を独立に決定できない場合だけ human_escalation 候補になる。

## 手順 (Procedure)

### Step 0: 前提条件 (Preconditions)

1. Issue 本文と必要コメントを取得する。`state/needs-human` / `state/done` は presentation-only / non-authoritative metadata（#2084）であり、それら label の単独付与だけで hard stop としない。停止には別 authority — OWNER の明示指示、または `human_judgment_required`（scope-signal-guard 等の別 authority）— が必要である。`state/done` の代替として GitHub native Issue `closed` state（`gh issue view --json state`）を参照する。
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

root checkout（canonical main root / default branch）から anchor comment を指定して preflight を実行する場合は、上記の直接 wrapper 呼び出しではなく、origin lane を明示する正規の privileged executor profile を使う。`preflight.run.with_anchor` は URL だけを扱う unlabeled profile であり、human origin を推定せず fail-closed にする。direct human context は `preflight.run.with_human_context`、agent report は read-only の `preflight.run.with_agent_report` を使う。以下は direct human context 用の厳密な token 列（`--locked` を含まない）そのものであり、`uv run --locked` governance policy の対象ではない:

<!-- policy-example --><!-- 以下は方針の例を示すコメントであり、実行対象のコマンド構文には影響しない -->
```bash
uv run --locked python3 scripts/agent-guards/skill_runtime_exec.py \
  --command-id preflight.run.with_human_context \
  --issue-number <N> \
  --repo <owner/repo> \
  --anchor-comment-url <canonical GitHub issue comment URL>
```

`--anchor-comment-url` は `https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<M>` の canonical shape のみを受け付け、`--issue-number` / `--repo` と URL 内の owner/repo/issue 番号が一致しない場合は拒否される（context-binding）。同一 URL を human / agent の両 lane に渡すこと、または unlabeled URL を human origin として扱うことは fail-closed である。`preflight.run` 自体の argv / placeholders / execution_class はこの sibling profile の追加によって一切変更されない。

#### Step 0g: trusted contract update（main control-planeを実行する親control-plane限定）

`preflight.run.with_human_context` が trusted contract patch plan を得た後だけ、main control-plane は canonical main root / default branch から次の明示phaseを実行できる。agent report / unlabeled anchor はこの mutation phase に到達できない。preflight entryの `mutation: false` は変更せず、このphase以外に `--consume-contract-patch-plan` を渡してはならない。

```bash
uv run --locked python3 scripts/agent-guards/skill_runtime_exec.py \
  --command-id contract_update.run.with_human_context \
  --issue-number <N> \
  --repo <owner/repo> \
  --anchor-comment-url <canonical GitHub issue comment URL>
```

このphaseは既存patch planをtransaction-localにconsumerへ渡し、candidate static readiness、controlled transaction、final readback、fresh preflight/review/readiness入力までを一続きに実行する。subagent / isolation worktree はこのcommandを直接実行しない。新しい永続schema、receipt、publisher、state storeは作らない。

wrapper の出力フィールドを確認する:

**canonical stdout フィールド（機械可読）:**
- `STATUS: pass | warn | needs_fix | blocked | environment_failure` — 常に出力される
- `NEXT_ACTION: proceed | proceed_with_notes | apply_deterministic_repair | human_judgment_required | fix_environment | issue_editor_required` — 常に出力される（`issue_editor_required` は `--consume-contract-patch-plan` 経路限定。下記「`NEXT_ACTION: issue_editor_required` 受信時」参照）
- `MUST_READ:` — 読むべきパス一覧（空の場合は省略）
- `COMMANDS_JSON:` — full command spec objects（canonical machine-consumable、空の場合は省略）
- `COMMANDS_DISPLAY:` — human-readable display（display_only=true、空の場合は省略）
- `BLOCKERS:` — ブロッカーコード一覧（空の場合は省略）
- `ARTIFACT:` — 書き込まれた artifact の key: path 一覧（空の場合は省略）。`STATUS: needs_fix` の場合は `repair_diagnostics` / `repair_candidate_body` も含まれる（Issue #2016 iteration-3 P1-1。`repair_action.diagnostics_artifact` / `.candidate_body_artifact` と同一パスを canonical artifact map からも参照可能にする）
- `REPAIR_ACTION:` — versioned `repair_action` disposition（Issue #2016。`STATUS: needs_fix` の場合のみ出力される。`disposition: auto_apply_safe` と diagnostics/candidate body artifact パス・original/repaired SHA を含む）

**`NEXT_ACTION: issue_editor_required`（Issue #2048 Scope Delta）:**
`contract_update.run.with_human_context` 経由の `--consume-contract-patch-plan` 実行が `full_rewrite_required` disposition を検出した場合（承認済み trusted anchor scope reframe に非空の `allowed_path_deltas` があるが、派生 `CONTRACT_PATCH_PLAN_V1.operations[]` が空で section-bound patch を materialize できない場合）に返る。このとき:
- `contract_update.run.with_human_context` を再実行しない（no-progress な同一 mutation の再試行は禁止）
- scope-reframe comment を再投稿しない（trusted anchor は既に Issue 上に存在するため新規 comment は不要）
- 既存の `issue-editor` / `edit-issue` controlled transaction route（Step 4 相当）へ handoff し、Issue 本文の完全な rewrite を行う
- handoff 後の mutation が完了しても、body/title readback・fresh `preflight.run` 再実行・fresh Step 2 review・fresh readiness が全て完了するまで implementation を許可しない（`_bounded_contract_update_handoff()` の既存 post-update gate と同じ 6-gate 基準を満たすまで `NEXT_ACTION: proceed` 相当の implementation authorization に進まない）

**`STATUS: needs_fix` / `NEXT_ACTION: apply_deterministic_repair`（Issue #2016）:**
`repair_issue_contract.py` が既知の safe な deterministic repair（disposition: `auto_apply_safe`）を1件以上検出した場合、`run_refinement_preflight.py` は generic blocker を追加せず `STATUS: needs_fix` / `NEXT_ACTION: apply_deterministic_repair` を返す。この orchestrator（issue-refinement-loop）は現時点では `apply_deterministic_repair` の auto-apply consumer を持たない。したがって `NEXT_ACTION: apply_deterministic_repair` を受信した場合は、Issue 本文への実際の auto-mutation は行わず、`STATUS: blocked` と同様に human 判断待ちの informational route として扱う（`ARTIFACT:` の `repair_action.diagnostics_artifact` / `repair_action.candidate_body_artifact` を人間が参照できるようにするだけで、`decide_next_loop_action.py` 等の rewrite loop router のロジック自体はこの Issue では変更しない）。実際の auto-mutation（controlled `issue-editor` transaction・stale hash guard・GitHub readback・fresh preflight 再実行）は別 Issue で扱う。

**非 canonical / 抑制フィールド:**
- `SUMMARY` — 人間向け prose、オーケストレーターは consume しない
- `DO_NOT_READ` — 予約済み（現在は常に空）、consumers は欠如に依存してはならない
- `EVIDENCE` — raw issue body / comments は stdout に出力されない（artifact のみ）

**warn (exit 1) の定義:**
planner exit 0 かつ `fail_closed.required == false` かつ `decisions.*.confidence` に `"unknown"` が 1 つ以上含まれる場合、`STATUS: warn` / exit 1 を返す。human note が必要だが blocking ではない。`NEXT_ACTION: proceed_with_notes` に従って継続できる。

- `NEXT_ACTION:` に従って後続ステップを決定する
- `ARTIFACT:` の `refinement_preflight_result_v1` パスから `fail_closed` / `decisions` を参照する
- `ARTIFACT:` の `planner_input` パスで planner へ渡した stdin JSON を確認できる

`STATUS: blocked` または `STATUS: environment_failure` の場合は停止し、人間判断へ送る。ここでの `STATUS: environment_failure` は Step 0f preflight の repository/environment failure であり、Step 1b の external evidence provider transport status とは別である。`investigation_policy` / `web_research_policy` / `scope_signal_guard` / `follow_up_materialization` の判定は planner を SSOT とし、このファイルで prose 再判定しない。

`scope_signal_guard.triggered: true` が含まれる場合でも、preflight 完了直後は
`decide_next_loop_action.py` を呼ばない（#1873: `ISSUE_REFINEMENT_PHASE_STATE_V1`
の formal phase-gate は撤去された。呼び出しタイミングの制御は orchestrator が
Step 順序に従って行う）。planner の `investigation_policy` / `web_research_policy`
に従って Step 1 / Step 1b / Step 2 へ進む。

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

web-researcher は AGY-first / native Web fallback を内部で完結する。critical claim に一次資料エビデンスを示せず、両 route でも検証不能またはハルシネーション疑いと判定した場合だけ `human_escalation` に倒す（Step 5）。provider telemetry / provenance trace の不足だけを escalation 理由にしてはならない。

Step 1 と Step 1b の完了後、main thread は current repository state、実 diff、実 test
等の Step 1 evidence から `repository_decision` を組み立て、planner の
`critical_external_claims[].role` と `WEB_RESEARCH_RESULT_V1` を
`web_research.route` registry entry で join する。`non_dispositive` は repository
evidence が requested disposition を独立に決定すると確認できた場合だけ使う。

join result が `next_action: proceed` または `proceed_with_notes` なら Step 2 へ進む。
後者は `transport_status: environment_failure` / `semantic_verdict: null` と
unresolved risk を記録するが、それだけで Step 5 に進めない。dispositive claim が
未解決、または repository decision が inconclusive の場合だけ
`human_judgment_required` として Step 5 へ進む。web-researcher の retry/fallback、
grounding trace、citation/provider provenance をこの consumer が再実装・保存しては
ならない。consumer は `verification_route` が producer 側で追加された未知の成功
route（例: `native_web`）であることを理由に reject してはならない — `status: ok`
かつ `verification_route` が非空であれば transport/grounding は成功したとみなし、
個々の route 文字列は informational として扱う。

詳細は `references/web-research-routing.md` を参照する。

### Step 2: レビュー (Review)

**producer I/O は root-owned、compact envelope 生成も含め単一 producer（Issue #2049 / merged PR #2135、AC8 SSOT を #2054 が拡張）**: live body fetch・body SHA 固定・`check_issue_contract` / `contract_readiness_check` / merge_readiness の実行・`ISSUE_REVIEW_RESULT_COMPACT_V2` compact envelope の生成・full artifact / compact artifact の canonical artifact directory への保存は、すべて orchestrator（main thread）が
`.claude/skills/issue-refinement-loop/scripts/run_root_review_pipeline.py produce --issue-number <N> --repo <owner/repo>`
（`root_review_pipeline.produce`, command_registry.py 登録済み、外部 CLI 契約は #2054 でも変更しない）を実行して行う。`produce` の内部実装は #2054 で `.claude/skills/issue-refinement-loop/scripts/reviewer_transport.py`（V2 契約 SSOT）へ委譲し、`ISSUE_REVIEW_RESULT_COMPACT_V1`（9行、`EVIDENCE` あり）ではなく `ISSUE_REVIEW_RESULT_COMPACT_V2`（`ATTEMPT_ID`/`ARTIFACT_SHA256` を含む exact 11行 grammar）を生成・永続化する。V1 producer（`compact_review_result.py` の compact 生成関数）は retired であり、同一 commit での atomic cutover に downgrade fallback はない。`produce` の JSON 出力には `compact_result.stdout_lines`（既に render・永続化済みの `ISSUE_REVIEW_RESULT_COMPACT_V2` 11 行）が含まれる。`issue-reviewer` SubAgent（`.codex/agents/issue-reviewer.toml`, `default_permissions: loop-protocol-readonly`）は orchestrator からこの `compact_result.stdout_lines` を明示的な read-only input として受け取り、そのまま自身の最終出力として relay するだけで、producer I/O を一切行わない。

これは Issue #2049 の root-owned pipeline アーキテクチャ（child agent は producer I/O を一切行わない）を維持したまま、#2054 が compact envelope の wire format を V1 から V2 へ atomic cutover した変更である。`run_root_review_pipeline.py` は #2054 が新設する第二の producer pipeline ではなく、既存の `classify_child_stdout()` / `retry_once_on_transport_failure()` / `readback_persisted_artifact()` の内部実装を `reviewer_transport.py` へ委譲する形で単一の V2 契約実装へ統合されている（AC8）。ステップ単位の所有権表は `.claude/skills/review-issue/SKILL.md` の「Producer I/O ownership」節を参照する。

**child stdout が 0 byte または malformed（非空だが不正）のときの扱い（AC4/AC5/AC6）**: orchestrator は `issue-reviewer` の raw stdout が 0 byte のまま返った場合でも、必ず `validate_review_compact_output.py`（内部実装は V2 契約 SSOT の `reviewer_transport.validate_compact_v2()` へ委譲、V2 grammar 専用）を呼ぶ（呼ばずに再試行やスキップをしない）。バリデータは `empty_input` violation に `classification: reviewer_transport_failure` を付与する。この classification を受けた場合、orchestrator は root producer（`issue-reviewer` SubAgent 呼び出し）を一度だけ再実行する。再実行後も `empty_input` / `reviewer_transport_failure` のままであれば、それ以上再試行せず `NEXT_ACTION: human_judgment_required` として Step 5 (human_escalation) へ倒す（`run_root_review_pipeline.retry_once_on_transport_failure()` が同じ一度だけ再試行のセマンティクスを実装する。この関数の `classify_child_stdout()` は V2 契約 SSOT の `reviewer_transport.validate_compact_v2()` を直接呼ぶ単一の実行経路であり、非空だが malformed な stdout も同じ `reviewer_transport_failure` として一度だけ再試行される — 別の簡易 classifier を持たない）。transport failure（spawn / timeout / signal / empty output / malformed output / artifact 書き込み・整合性失敗を含む）は常に `STATUS: environment_failure` / `semantic_verdict: null` に収束し、`human_judgment_required` として記録しない。symlink／root escape／attempt mismatch／hash mismatch は `integrity_failure` として区別する。

消費側契約 (consumer contract): `ISSUE_REVIEW_RESULT_COMPACT_V2`（正本 (SSOT): `.claude/skills/issue-refinement-loop/scripts/reviewer_transport.py`）。V1（`ISSUE_REVIEW_RESULT_COMPACT_V1`、`compact_review_result.py`）は retired であり、partial deployment・downgrade fallback は禁止する。

**validator-first 順序（AC4/AC6、routing table より前に評価する）**: orchestrator は approve / needs-fix いずれの経路でも、SubAgent（child）stdout の raw bytes を consume する前に、必ず `run_root_review_pipeline.py classify-child-stdout`（`--issue-number` / `--artifact-root` / `--repo` / `--expected-body-sha256` を渡す）へ child の raw stdout bytes をそのまま（re-transcribe せず）渡す。この単一エントリポイントが内部で `validate_review_compact_output.py`（V2 grammar 判定）→ `reviewer_transport.verify_artifact()`（trusted artifact root FD を起点に `O_DIRECTORY|O_NOFOLLOW` で component-wise open、同一 accepted leaf FD から一度だけ bounded raw bytes を読み、その同じ raw bytes で SHA-256・strict JSON・schema/repository/issue/reviewed body SHA/invocation/attempt を検証する。`Path.resolve()` → `stat()` → 別 `open()` という resolve/stat と open を分離した security fallback は行わない）→ `reviewer_transport.verify_wire_matches_artifact()`（検証済み artifact の `semantic_result` から wire を再導出し、child が relay した wire と byte 単位で一致することを確認する。ARTIFACT/ARTIFACT_SHA256 を変えずに VERDICT/BLOCKERS/NEXT_ACTION だけを自己整合的に書き換えた wire はここで `integrity_failure` として reject される）の順に評価する。**この一連の検証が完了する前に `VERDICT` / `NEXT_ACTION` / `ARTIFACT` を読んではならない。** `classification` が `ok` 以外（`reviewer_transport_failure` または `integrity_failure`）の場合は routing を `human_judgment_required` に固定する（fail-closed。`integrity_failure` は再試行しない）。`classification: ok` の場合のみ、`normalized_payload` を根拠に以下の routing table を評価する:

- `VERDICT: approve` → Step 4.5 へ
- `VERDICT: needs-fix` → `decide_next_loop_action.py`（rewrite 後 / next-action 決定時に呼ぶ、下記「LOOP_STATE」参照）へ VERDICT をそのまま渡す（**#1873: 旧 Step 2a Replay Arbitration は撤去された** — orchestrator は VERDICT を独立に再計算せず直接信頼する）
- full structured data は parent-verified V2 artifact（`ARTIFACT: compact_review_result_v2=<path>`）から取得する（main context には返らない、validator と artifact binding 通過後のみ参照可）

anchor comment により stale approval を無効化する場合も、raw snapshot は Step 4 に渡さず、正規化済み `anchor_comment_feedback` だけを渡す。

**重要**: `review` phase（rewrite 前）では `decide_next_loop_action.py` を呼んではならない。
`review` phase での routing は VERDICT に基づいて直接行う（承認なら次段階へ、要修正なら書き直しへ）:

- `VERDICT: approve` → Step 4.5 へ（承認）
- `VERDICT: needs-fix` → Step 4（rewrite、書き直し）へ

`decide_next_loop_action.py` は rewrite 後 / next-action 決定時にのみ呼ぶ:

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py \
  --loop-state-file <loop_state_path> \
  --review-result-verdict <approve|needs-fix>
```

`decide_next_loop_action.py` は呼ばれた時点で `scope_signal_guard.triggered: true` を
常に hard-stop 判定する（呼び出しタイミングの制御が orchestrator 側の責務であり、
スクリプト自身は phase 概念を持たない。AC4 / #919 回帰維持）。

### Step 4: 書き換え (Rewrite)

`issue-editor` SubAgent に opaque forwarding payload を渡して本文を更新する（Issue #1734: `issue-author` は `issue-creator` / `issue-editor` に分割済み。既存 Issue の書き換えは `issue-editor` が担う）。AC/VC の baseline fail expectation と review 時の扱いを取り違えないこと。詳細な reflection guard は `references/ac-vc-reflection.md` を参照する。

issue-editor 起動前に、現在本文に対して pre-edit static readiness check を実行する。

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
  action: invoke_issue_editor
  readiness_errors: []
exit_code_1:
  status: needs_fix
  action: invoke_issue_editor_with_readiness_result
exit_code_2:
  status: human_judgment
  action: skip_issue_editor_and_go_step5
exit_code_3:
  status: input_or_runtime_error
  action: human_escalation
```

消費側契約 (consumer contract): `ISSUE_AUTHOR_RESULT_COMPACT_V1`（正本 (SSOT): `.claude/skills/issue-refinement-loop/scripts/compact_author_result.py`）

- `STATUS: ok` / `BODY_HASH: <sha256>` → 更新成功、`NEXT_ACTION: proceed` で Step 2 に戻る
- `STATUS: no_change` → 変更なし、`NEXT_ACTION: proceed` で Step 2 に戻る

**final review gate（Issue #2049 AC10）**: `STATUS: ok` / `BODY_HASH` readback により本文更新が確認できた場合にのみ Step 2 の "final review"（この body revision に対する terminal VERDICT を決める review pass）を実行する。readback が未確認、または `run_root_review_pipeline.py readback` が `verdict_identity: false` を返す（persisted artifact が regular file でない / symlink である / strict JSON でない / `body_sha256` が一致しない / `verdict` が一致しない、のいずれか）間は final review を実行しない。`run_root_review_pipeline.py gate-final-review --artifact-path <path> --expected-body-sha256 <sha> --expected-verdict <verdict> --remote-update-ok` が `final_review_allowed: true` を返した場合にのみ次段階（Step 4.5）へ進める。
- `STATUS: failed` → 修正失敗、`NEXT_ACTION: human_judgment_required`、Step 5 human_escalation へ
- `partial_failure` は廃止。issue-editor は `ok` / `no_change` / `failed` の 3 値のみを返す。
- full mutation result は `ARTIFACT:` パスから取得する（main context には返らない）

rewrite ループの反復ごとに、checker 実行後に `scripts/decide_rewrite_route.py` を呼び出して `max_rewrite_attempts` 超過・body hash 変化なし・missing set 単調減少なしを runtime で強制し、`route`（`continue_rewrite` / `proceed_to_review` / `human_judgment_required`）に従って routing する。invocation 手順・state 永続化・`human_judgment_required` 連動は `references/termination-policy.md` の「Rewrite Loop Runtime Router（#664）」セクションを SSOT とする。orchestrator は attempt 数や no-progress を prose で再判定しない。

**#2048 regression（承認済み scope reframe が empty operations[] のケース、Scope Delta で production reachability を修正）**: 承認済み trusted anchor scope reframe（`scope_delta_status: approved_by_trusted_anchor` かつ `allowed_path_deltas` 非空）から派生した `CONTRACT_PATCH_PLAN_V1.operations[]` が空の場合、`decide_rewrite_route.py` の `decide_scope_reframe_contract_route()` が `contract_update` ではなく `issue_editor_required`（reason_code: `approved_scope_requires_full_contract_rewrite`）へ route する。

唯一の production 呼び出し元は `run_refinement_preflight.py::consume_trusted_anchor_contract_patch_plan()`（`contract_update.run.with_human_context` の `--consume-contract-patch-plan` 実行がこの関数を呼ぶ）である。この consumer 境界が `known_context["scope_delta_decision"]`（`_classify_anchor_scope_reframe()` が生成する trusted anchor 分類結果）を fail-closed に再検証（型・binding・anchor 一致）した上で `allowed_path_deltas` を抽出し、`scope_signal_delta.run_trusted_anchor_iteration_zero()` の `allowed_path_deltas` / `already_reflected_in_body` 引数へ実際に渡す。`operations[]` の producer が存在しない場合（freeform directive に exact path literal がなく `classify_scope_delta_authority()` が human_escalation へ倒れる場合等）は、`consume_trusted_anchor_contract_patch_plan()` の呼び出し元が empty-operations の `CONTRACT_PATCH_PLAN_V1` を合成し consumer へ渡す。

**#2086 AC3**: `expands_allowed_paths` boundary が理由で `human_escalation` に倒れた operator-selected human-context directive（trusted OWNER/MEMBER/COLLABORATOR、`with_human_context` lane）に対しては、mutation を行わない前に `codebase-investigator` SubAgent（read-only）へ委譲し、directive が意味論的に指す exact repository-relative path を current main から導出する。導出できた場合は `classify_scope_delta_authority()` の `investigation_derived_path_literals` キーワード引数へ渡して再判定する（`references/anchor-comment-handling.md` の「Operator-Selected Human-Context 継続」参照）。導出できない場合、または導出結果でも `contract_patch_plan.operations` が空のままの場合は、上記の `NEXT_ACTION: issue_editor_required` full-rewrite lane へ進む。人間へ手書き `ANCHOR_SCOPE_REFRAME_V1` の再入力を要求してはならない。

empty operations は無条件に `issue_editor_required` を意味しない。承認済み `allowed_path_deltas` が現在の Issue 本文の `## Allowed Paths` セクションに既に全て反映済みの場合は `proven_no_change`（通常の `no_change` no-op、rewrite_route は付与されない）として扱われる。型不正（`operations` が list でない等）や binding 不一致（`anchor_comment_url` / `anchor_comment_hash` が今回の呼び出しと一致しない）は fail-closed に `None` 扱いとなり、同様に `issue_editor_required` へは昇格しない。

`plan_refinement_loop.py` 側にも同じ判定を `decisions.scope_delta_decision.rewrite_route` として echo する opt-in 経路（`known_context.scope_delta_decision.operations` が渡された場合限定）があるが、これは診断用の echo であり canonical な mutation-phase routing は上記 consumer 境界が担う。

`full_rewrite_required` が検出された場合、`run_refinement_preflight.py` の wrapper は通常の `STATUS` 由来 `NEXT_ACTION` 判定を上書きし `NEXT_ACTION: issue_editor_required` を stdout・result artifact の両方に反映する（`contract_update.status` は既存の `applied`/`no_change`/`rebased`/`failed` 4 値のみを使い、`no_change` を full-rewrite-required の意味に流用しない）。この受信後の orchestrator 手順は Step 0g の「`NEXT_ACTION: issue_editor_required` 受信時」を参照する。

### Step 4.5: 子Issue/follow-up の実体化 (Materialization)

delivery-rollup parent の child materialization gate と、approve 後の follow-up 起票候補は `references/follow-up-materialization.md` を参照する。dedupe は title ではなく `dedupe_key` で行う。

### Step 5: 終了処理 (Termination)

終了条件、`human_escalation` 経路、scope change signal 停止、loop termination table は `references/termination-policy.md` を参照する。

`approved` 終了時、`issue_kind: implementation` の Issue（`impl-review-loop` への handoff を伴う通常実装 Issue）は `LOOP_HANDOFF_RESULT_V1` marker を終了コメントに出力する（形式・routing rules は `references/termination-policy.md#LOOP_HANDOFF_RESULT_V1` 参照）。出力は `<!-- LOOP_HANDOFF_RESULT_V1 -->` HTML comment と fenced YAML block の 2 要素。

**適用範囲外（`issue_kind: parent`、delivery-rollup parent を含む、#1914）**: `issue_kind: parent` の Issue が `approved` 終了する場合、本 marker は出力しない（`impl-review-loop` への handoff が発生しないため）。plain Markdown の終了要約のみを投稿し、delivery-rollup parent の場合は要約に `Final Gate: not applicable` と reason code を明記する。詳細は `references/termination-policy.md` の `LOOP_HANDOFF_RESULT_V1` セクション（適用範囲節）を SSOT とする — 本ファイルはそれを矛盾なく要約するのみで、独自の無条件規則を持たない。

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

#1873（bounded review loops）: `render_termination_report.py`（`TERMINATION_REPORT_INPUT_V1` を
検証する renderer）は撤去された。orchestrator は `decide_next_loop_action.py` の出力
（`STATUS` / `NEXT_ACTION` / `TERMINATION_CAUSE` / `BLOCKERS`）と loop の経緯から短い
plain markdown の summary を直接組み立て、`--body-file` または stdin で渡す。

```bash
# summary.md に組み立て済みの plain markdown を書いてから渡す
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py \
  --issue-number 42 --repo <owner/repo> --body-file summary.md
```

`human_escalation` の summary では、termination cause が未確定の場合 `human_judgment_required`
へ正規化する（詳細は `references/termination-policy.md` の「termination_cause 正規化ルール」）。

`publish_termination_report.py` は以下の責務を持つ:

1. body が空でないことを確認する（空文字列は `empty_body` で fail-closed）
2. `issue_comment.publish` controlled mutation lane（Issue #1633）経由で GitHub issue comment を投稿する（raw `gh issue comment` を直接呼ばない）
3. gh 呼び出しが失敗またはタイムアウトした場合は fail-closed で終了し、reason_code / timestamp をローカル artifact に記録する

詳細な publisher 仕様は `.claude/skills/issue-refinement-loop/scripts/publish_termination_report.py` を参照する。

## ISSUE_EXECUTION_DECISION_V1 ハンドオフ契約 (#1677)

**現在 production 接続済みの契約**（PR #1767 owner review + Scope Delta 反映後、#1873 でコンシューマ集合を更新）: `scripts/validate_issue_execution_decision.py` が standalone canonical module（`validate_schema()` / `validate_semantics()` を明示的に分離、schema-first の `validate_issue_execution_decision()`、`project_issue_execution_decision_ref()`、legacy adapter・migration envelope helper）として存在し、`run_refinement_preflight.py`（downstream consumer）が同一モジュールを import して呼び出す。`plan_refinement_loop.py` は `build_issue_execution_decision()` の producer だが、自身の出力を自己検証しない（#1873: Replay Arbitration 撤去に伴い内部整合性チェックを削除。downstream consumer が独立に検証する）。import 失敗は fail-closed（silent skip 禁止）。

**未接続（follow-up）**: `REFINEMENT_LOOP_PLAN_V1`/`LOOP_STATE_V1`/`LOOP_HANDOFF_RESULT_V1` の required 化（現状は additive/optional）。genuine な `depends_on`/`duplicate`/`absorb` を GitHub native dependency・明示的 duplicate marker・方向付き Machine-Readable Contract から直接構築する結線（現状は `ISSUE_SCOPE_ROLLUP_PLAN_V2` からの derivation を撤去した保守的な `deferred` fallback のみ）。

downstream skill（impl-review-loop・implement-issue・issue-contract-review・open-pr）は `downstream_policy`（`semantic_reclassification: forbidden` / `freshness_validation: required` / `stale_action: rerun_issue_refinement`）に従い、semantic relation を再分類しない。stale 検出時は issue-refinement-loop を再実行する。

**semantic relation の情報源について**: `ISSUE_SCOPE_ROLLUP_PLAN_V2` は coordination plan であり semantic dependency graph ではない。方向未確定の `sequential_required` や `merge_into_current_pr` 等の suggested_action は、depends_on/duplicate/absorb relation へ自動変換しない（誤変換は PR #1767 owner review で指摘され撤去済み）。`proceed_with_coordination` のみ非committal な `coordinates` relation に変換する。それ以外の曖昧な signal は `execution.state: deferred` にして human 判断を要求する。

**Canonical schema の digest provenance / legacy compatibility metadata**（AC10）: `schemas/issue_execution_decision_v1.schema.json` に additive/optional な `provenance`（`policy_version`・producer/collector provenance・`canonicalization_id`・`digests.{source_manifest_sha256,semantic_decision_sha256,artifact_sha256}`・`legacy_compatibility.{legacy_schema_identifiers,supported_consumer_versions}`）と `migration`（`phase: dual_write | equivalence | dual_read | new_authoritative | legacy_removed`・`legacy_digest`・`new_digest`・`equivalence_result`・`producer_version`・`consumer_capability`）ブロックを追加した。`equivalence` phase の digest 不一致は `validate_issue_execution_decision.validate_migration()` が fail-closed で拒否する（AC13）。legacy `graph.nodes/graph.edges` + `execution.target_state/predecessor_issue_numbers/reason_codes` からの adapter は `validate_issue_execution_decision.adapt_legacy_graph_to_v1()` が実装し、canonical output は常に V1 へ正規化する。詳細は `references/refinement-loop-plan-output.md` の `issue_execution_decision` 節を参照する。

## 参照マップ (Reference Map)

| topic | primary reference |
|---|---|
| anchor comment schema | `schemas/anchor_comment.schema.json`（Issue #1873: `loop_state.schema.json` から抽出） |
| loop state field definitions（historical） | `references/loop-state.md` |
| anchor comment handling | `references/anchor-comment-handling.md` |
| scope signal guard | `references/scope-signal-guard.md` |
| AC/VC reflection | `references/ac-vc-reflection.md` |
| follow-up materialization | `references/follow-up-materialization.md` |
| web research routing | `references/web-research-routing.md` |
| termination policy | `references/termination-policy.md` |
| planner output contract | `references/refinement-loop-plan-output.md` |
| scope rollup preflight | `references/scope-rollup-policy.md` |
| command registry | `scripts/command_registry.py` — `ISSUE_REFINEMENT_COMMAND_REGISTRY_V1` |
| architecture review / design 判断・failure mode の詳細（`derived_design_note`。本 entrypoint と矛盾する場合は本 entrypoint が正本、#1876） | `docs/dev/workflows/issue-refinement-loop-design.md` |

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
