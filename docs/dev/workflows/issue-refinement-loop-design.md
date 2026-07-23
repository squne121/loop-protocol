---
design_doc_schema_version: v1
ssot_classification: derived_design_note
summary_ja: "この文書は issue-refinement-loop の設計判断・状態遷移・サブエージェント契約・失敗モードと復旧手順を集約した唯一の正本（SSOT）であり、アーキテクチャレビューや契約移行の際に参照する。"
canonical_sources:
  - docs/dev/workflow.md
  - docs/dev/agent-skill-boundaries.md
  - .claude/skills/issue-refinement-loop/SKILL.md
  - .claude/agents/issue-author.md
  - .claude/agents/issue-reviewer.md
  - .claude/agents/codebase-investigator.md
  - .claude/agents/web-researcher.md
conflict_rule: canonical_sources_win
loaded_when:
  - architecture review of issue-refinement-loop
  - contract migration affecting planner or reviewer interfaces
  - failure-mode update for refinement loop
  - onboarding to understand planner/orchestrator boundary
not_loaded_when:
  - normal loop execution (runtime)
  - routine issue refinement
  - any SubAgent execution within the loop itself
summary_budget: "<= 1200 chars"
---

# issue-refinement-loop 詳細設計書

> **derived_design_note**: 本文書は `canonical_sources` に列挙された正本を補足する派生ノートである。
> 本文書と正本が矛盾する場合、常に正本が勝つ（`conflict_rule: canonical_sources_win`）。

## Status（状態）

| フィールド | 値 |
|---|---|
| 設計書バージョン | v1 |
| 対象スキル | `issue-refinement-loop` |
| 最終更新 Issue | #1012 |
| 状態 | active |

## Purpose（目的）

Issue 本文の品質を反復改善する `issue-refinement-loop` の設計判断・責務境界・停止条件・人間エスカレーション分類を、正本への参照リンクと共に記録する。

本設計書が説明する主要な設計判断:

- planner が判定の正本であり、orchestrator は prose 再判定しない
- web research 結果は orchestrator が opaque に扱う
- anchor comment の raw snapshot を writer に直接流さない
- scope change signal で停止する条件
- follow-up materialization の dedupe key 設計
- `state/blocked` / `state/queued` hygiene の範囲
- `max_iterations` と `human_escalation` の既定継続条件
- human escalation 分類表

## Non-goals

- `plan_refinement_loop.py` のロジック詳細（planner の SSOT はスクリプト本体）
- `web-researcher` / `gemini-cli-headless-delegation` の retry/fallback 設計詳細
- `.claude/agents/*.md` の責務移動
- SubAgent 定義全文の複製（SSOT 分裂を招く）
- SKILL.md 手順全文の複製

## Invocation Contract（起動契約）

| フィールド | 内容 |
|---|---|
| 呼び出しトリガー | 「Issue ◯◯ を改善して」「refinement loop」 |
| 必須入力 | `issue_number` |
| 任意入力 | `max_iterations`（既定 3）、`anchor_comment_url` |
| 出力 | `LOOP_STATE` YAML（termination_reason 含む） |
| 前提条件 | `state/needs-human` / `state/done` 非存在 |
| 完了後 | `issue-contract-review` による go 判定後に `impl-review-loop` へ引き継ぐ |

### 入力スキーマ参照

正本: `.claude/skills/issue-refinement-loop/SKILL.md` の `## Inputs` セクション

## Workflow Topology（ワークフロー構成）

```
[Step 0: Preconditions / planner input assembly]
       ↓
[Step 0f: plan_refinement_loop.py → REFINEMENT_LOOP_PLAN_V1]
       ↓ fail_closed.required == true → 人間判断（停止）
[Step 1: Investigation]      → codebase-investigator (conditional)
[Step 1b: Web Research]      → web-researcher (conditional)
       ↓
[Step 2: Review]             → issue-reviewer (review-issue skill)
       ↓
 approve → Step 4.5 (child/follow-up materialization) → Step 5 (approved)
 needs-fix
   ├─ iteration + 1 >= max_iterations → Step 5 (human_escalation)
   └─ else → Step 4 (issue-author) → next iteration
```

設計判断: Step 3（adversarial review）と Step 1.5（spec document review）は採用しない。Step 番号は履歴互換のため保持する。

### Topology 設計根拠

1. **planner-first**: `REFINEMENT_LOOP_PLAN_V1` が investigation/web_research/scope_signal の要否を決定する。orchestrator は planner 出力を consume するだけで prose 再判定しない（split-brain 防止）。
2. **`max_iterations=3` の既定**: refinement loop は `issue-refinement-loop/SKILL.md` の `loop_policy` と一致する。needs-fix 受け取り時は `iteration + 1 < max_iterations` で自動継続、`iteration + 1 >= max_iterations` で `human_escalation` により停止する。
3. **anchor comment の正規化**: raw snapshot を reviewer/writer に直接流すと hallucination リスクが高まるため、orchestrator が正規化済み `anchor_comment_feedback` のみを渡す。

## State Model（状態モデル）

`LOOP_STATE` の完全フィールド定義は `.claude/skills/issue-refinement-loop/references/loop-state.md` を参照し、`scripts/decide_next_loop_action.py` の `next action` 制御へ委譲する。

以下はフィールドの所有者と変更ルールのサマリ:

| フィールド グループ | Owner | 変更タイミング |
|---|---|---|
| `iteration` / `last_verdict` | orchestrator | 各イテレーション完了後 |
| `anchor_comment.*` | orchestrator (Step 0 で確定) | anchor URL 提供時のみ更新 |
| `investigation_policy` | planner（Step 0f） | `REFINEMENT_LOOP_PLAN_V1` 消費時 |
| `web_research_policy` | planner（Step 0f） | `REFINEMENT_LOOP_PLAN_V1` 消費時 |
| `web_research.result` | opaque（web-researcher が所有） | orchestrator は link-only で保持 |
| `scope_signal_guard` | planner（Step 0f） | scope change 検出時に triggered: true |
| `termination_reason` | orchestrator（Step 5） | 終了時に確定 |

### State transition rules（状態遷移規則）

- `scope_signal_guard.triggered == true` かつ `excluded_by_anchor_reframe == false` → `termination_reason: human_escalation` で即停止
- `fail_closed.required == true`（planner 出力）→ 即停止
- `iteration + 1 >= max_iterations` → `termination_reason: human_escalation`
- `last_verdict == approve` → `termination_reason: approved`

## SubAgent Contract Matrix（サブエージェント契約表）

| SubAgent | Role | Producer | Consumer | Schema/version | Fields read by orchestrator | Opaque forwarded fields | Must-not-read fields | Mutation owner | Failure class | Verification fixture |
|---|---|---|---|---|---|---|---|---|---|---|
| `codebase-investigator` | リポジトリ内エビデンス収集 | codebase-investigator | orchestrator | `REPO_EVIDENCE_REF_V1` | `final_classification`, `verified_claims`, `unresolved_claims` | `raw_evidence[]` | `anchor_comment.raw_snapshot` | read-only（mutation 禁止） | `investigation_failed` | `.claude/skills/issue-refinement-loop/tests/` |
| `web-researcher` | 外部仕様調査 | web-researcher | orchestrator | `WEB_RESEARCH_RESULT_V1` | `status`, `failure_class`, `verification_route`, `claims`, `unresolved_risks` | `result`（raw は opaque） | `retry_log`, `attempt_log`, `fallback_state` | read-only（mutation 禁止） | `web_research_failed` | `.claude/skills/issue-refinement-loop/tests/` |
| `issue-reviewer` | Issue レビュー判定 | issue-reviewer | orchestrator | `ISSUE_REVIEW_RESULT_COMPACT_V1` | `status`, `verdict` | なし | domain judgment の再解釈禁止 | read-only（mutation 禁止） | `review_failed` | `.claude/skills/review-issue/` |
| `issue-author` | Issue 本文更新 | orchestrator | issue-author | `anchor_comment_feedback`（正規化済み） | `update_status`, `edit_result` | なし | `anchor_comment.raw_snapshot`（直接渡し禁止） | write（Issue body のみ） | `edit_failed` | `.claude/skills/edit-issue/` |

### SubAgent 設計上の注意

- orchestrator は `WEB_RESEARCH_RESULT_V1` の `retry/fallback/attempt_log` を読んではならない（`#394` の責務越境防止）
- `codebase-investigator` は `final_classification` の確定責務を持たない（orchestrator が main thread で確定）
- `issue-author` への入力は必ず `anchor_comment_feedback`（正規化済み）を使い、raw snapshot を直接渡してはならない

### Step 2a: 親ローカル replay 整合性束縛 V2（parent-local replay integrity binding、#1532）

`issue-reviewer` の `needs-fix` 判定に対する arbitration（Step 2a）は、child の isolation worktree が返す raw artifact / raw `findings` / `checker_evidence` / `deterministic_checks` を orchestrator が信用しない V2 契約へ移行した。

本節が提供するのは **parent-local replay integrity binding** であり、child SubAgent（同一 OS UID プロセス）の producer identity・署名・鍵管理・supply-chain provenance を証明する attestation ではない（Safety Claim Matrix の対象外。この保証範囲の違いを示すため「provenance attestation」という語は使用しない）。

- **child の出力**: `issue-reviewer` は `compact_review_result.py` 経由で `ISSUE_REVIEW_RESULT_COMPACT_V1` の needs-fix envelope（STATUS/VERDICT/SUMMARY/BLOCKERS/NEXT_ACTION/MUST_READ/EVIDENCE/ARTIFACT）に加えて、`REVIEWER_BLOCKER_CLAIM_V1`（`{schema, body_sha256, blockers: [...]}`、`reviewer_blocker_code`/`message`/`line_start`/`line_end` のみ）を 1 行返す。`findings` / `checker_evidence` / `deterministic_checks` はこの claim に一切含まれない（含めようとすると `additionalProperties: false` により拒否される）。child は `reviewer_claim_replay.py` を co-locate 実行せず、`REPLAY_VERDICT` 等の routing フィールドを一切返さない。
- **parent の replay**: orchestrator（parent）は `.claude/skills/issue-refinement-loop/scripts/parent_replay_binding.py` を使い、自ら取得・保存・readback した `readiness_result` / `vc_syntax_result` / `vc_preflight_result` / `previous_state` / 現在の Issue body の raw bytes snapshot / identity（`repository_full_name` / `issue_number` / `refinement_session_id` / `iteration_id`）と、strict schema 検証済みの child `REVIEWER_BLOCKER_CLAIM_V1` を入力として `reviewer_claim_replay.analyze()` を in-process で再実行し、`PARENT_REPLAY_BINDING_ARTIFACT_V1`（`replay_next_state` と `binding_digest` を含む）を生成する。`findings` / `deterministic_checks` は常に空で構築されるため、`deterministic_backed` は parent 自身の readiness/vc-preflight/vc-syntax evidence からのみ導出される。`iteration_id` を `analyze()` に渡すため wall-clock 値は一切生成されない（同一論理入力は常に同一 digest）。child の raw artifact ファイルは一切読まない（#1472 isolation boundary を継承）。
- **V2 envelope の組み立て（Issue #1541）**: orchestrator は child の claim envelope テキスト（8 行 approve、または `REVIEWER_BLOCKER_CLAIM` を含む 9 行 needs-fix intermediate。この 9 行 grammar は V1/V2 final grammar と別物で、`emit_parent_review_envelope_v2.py` が strict 検証する）と上記 binding artifact を `.claude/skills/issue-refinement-loop/scripts/emit_parent_review_envelope_v2.py`（command registry `review_compact.emit_v2`）へ渡す。この producer は child claim の canonical digest を binding artifact の `input_digests.reviewer_blocker_claim_sha256` と照合し、binding artifact 自身の digest 自己整合性・identity（repository/issue/session/iteration/body）を検証したうえで、`PARENT_REPLAY_VERDICT` / `PARENT_REPLAY_ROUTING` / `PARENT_REPLAY_SHOULD_CONSUME` / `PARENT_REPLAY_BODY_SHA256` / `PARENT_REPLAY_NEXT_STATE`（canonical 1 行 JSON）/ `PARENT_REPLAY_BINDING_DIGEST` の 6 行を binding artifact からのみ決定論的に導出し、UTF-8・LF・末尾 LF ありの完全な `ISSUE_REVIEW_RESULT_COMPACT_V2`（15 行）を stdout に一度だけ出す（成功時のみ・部分出力なし）。routing に使われるのは `PARENT_REPLAY_*` のみであり、V1 の child 自己申告 `REPLAY_VERDICT` 等は producer 契約から廃止された。旧来 orchestrator が f-string で 6 行を手動追記する assembly（テスト専用 `_assemble_v2_envelope()` 相当）は production 経路から廃止されている。
- **V2 validator**: `validate_review_compact_output_v2()` は独立に供給された `PARENT_REPLAY_BINDING_ARTIFACT_V1`（必須引数、省略不可）を strict schema 検証・digest 再計算・identity/body 照合したうえで、envelope の `PARENT_REPLAY_*` 全フィールドと binding artifact の期待値を exact 照合する。不一致・不正形式・binding artifact 不在は `human_judgment_required` に fail-closed する。
- **state 永続化**: `reviewer_claim_replay_state_store.py --write-v2` は、`REVIEW_COMPACT_VALIDATION_RESULT_V2` の `schema` / `schema_version` / `envelope_kind` / `violations == []` / `validation_status: valid` / identity をすべて自ら再検証したうえでのみ `PARENT_REPLAY_NEXT_STATE` を永続化する（caller が組み立てた `{"validation_status": "valid", ...}` のみの偽装 payload は拒否される）。

`parent_replay_binding.py` の exact CLI 呼び出し例:

```bash
uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/parent_replay_binding.py \
  --reviewer-blocker-claim-file <child stdout の REVIEWER_BLOCKER_CLAIM を保存したファイル> \
  --readiness-result-file <parent が取得済みの ISSUE_CONTRACT_READINESS_RESULT_V1> \
  --previous-state-inline '<reviewer_claim_replay_state_store.py --read の結果、または "{}"' \
  --current-body-file <parent が取得した現在の Issue body raw bytes> \
  --issue-url <issue url> \
  --repository-full-name <owner/repo> \
  --issue-number <N> \
  --refinement-session-id <session id> \
  --iteration-id <parent が生成した iteration id>
```

`emit_parent_review_envelope_v2.py`（Issue #1541、command registry `review_compact.emit_v2`）の exact CLI 呼び出し例:

```bash
<child stdout（8 行 approve または 9 行 needs-fix intermediate）> | \
uv run --locked --offline --no-sync python3 .claude/skills/issue-refinement-loop/scripts/emit_parent_review_envelope_v2.py \
  --issue-number <N> \
  --binding-artifact-file <上記 parent_replay_binding.py の stdout を保存したファイル> \
  --repository-full-name <owner/repo> \
  --refinement-session-id <session id> \
  --iteration-id <parent が生成した iteration id> \
  --current-body-file <parent が取得した現在の Issue body raw bytes>
```

成功時は完全な 15 行 `ISSUE_REVIEW_RESULT_COMPACT_V2` を stdout に一度だけ出す（exit 0）。child intermediate / binding artifact のいずれかが contract-invalid の場合は stdout を空のまま exit 1、runtime/environment error は stdout を空のまま exit 2 とし、stderr に machine-readable diagnostic（`EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE`）を出す。approve envelope（8 行）は binding artifact を一切参照せず pass-through validation のみ行う。

## Artifact and Evidence Contract（証跡契約）

| Artifact | 生成者 | 保存先 | 形式 | 消費者 |
|---|---|---|---|---|
| `REFINEMENT_LOOP_PLAN_V1` | `plan_refinement_loop.py` | stdout（memory only） | JSON | orchestrator Step 0f |
| `ISSUE_REVIEW_RESULT_COMPACT_V1` | `issue-reviewer` | stdout（memory only） | YAML | orchestrator Step 2 |
| `LOOP_STATE` (final) | orchestrator | Issue コメント | YAML | human / impl-review-loop |
| anchor comment snapshot | orchestrator (Step 0) | `LOOP_STATE.anchor_comment` | 正規化 YAML | issue-author（正規化済みのみ） |

`decision: not_applicable`（docs-only 変更等）の場合は artifact 出力不要。

## Context Loading Policy（コンテキスト読み込み方針）

本設計書は以下の場面でのみロードする（`loaded_when` / `not_loaded_when` 参照）:

| シナリオ | ロード可否 | 理由 |
|---|---|---|
| architecture review | YES | 設計判断の参照が必要 |
| contract migration | YES | SubAgent Contract の変更影響確認 |
| failure-mode update | YES | 停止条件・escalation 分類の参照 |
| normal loop execution | NO | runtime overhead なし。正本 SKILL.md を参照 |
| routine issue refinement | NO | 派生ノートのロードは不要 |

### Progressive Disclosure（段階的開示）

本設計書の全文ロードが不要な場合の参照手順:

1. SubAgent 責務境界のみ → `## SubAgent Contract Matrix` のみ読む
2. 停止条件のみ → `## Failure Modes and Recovery` を読む
3. escalation 分類のみ → `## Failure Modes and Recovery` の Human Escalation Classification を読む

## Guardrails（ガードレール）

1. **planner SSOT 遵守**: orchestrator は `REFINEMENT_LOOP_PLAN_V1` の `decisions.*` を prose 再判定しない。planner が `required: false` と返した investigation/web_research は実行しない。
2. **anchor comment 正規化**: raw snapshot を SubAgent への入力として直接使用しない。必ず orchestrator が `anchor_comment_feedback` に正規化してから渡す。
3. **WEB_RESEARCH_RESULT_V1 の opaque 扱い**: retry/fallback フィールドを読まない。`status` / `failure_class` / `verification_route` / `claims` / `unresolved_risks` のみ orchestrator が読む。
4. **state/blocked hygiene の範囲**: `state/blocked` / `state/queued` の除去は「本 Issue への refinement 継続が確定した後」にのみ行う（Step 0 完了後）。
5. **fail-close**: `fail_closed.required == true` の場合は即停止し、人間判断を仰ぐ。

## Failure Modes and Recovery（失敗モードと復旧）

### Failure Mode 分類表

| Failure Mode | 検出タイミング | 停止条件 | Recovery アクション |
|---|---|---|---|
| `state/needs-human` 存在 | Step 0 | Hard stop | 人間判断。ラベル除去まで待機 |
| `fail_closed.required == true` | Step 0f | Hard stop | 人間判断へ escalate |
| scope change signal | Step 2 / Step 4 | `scope_signal_guard.triggered == true` かつ anchor reframe なし | `termination_reason: human_escalation` |
| investigation failure | Step 1 | `failure_class: investigation_failed` | 人間判断へ escalate（partial evidence で進めない） |
| web research failure | Step 1b | `failure_class: web_research_failed` | `skip_reason` に理由を記録し、web research なしで続行（critical claim がある場合は停止） |
| `iteration + 1 >= max_iterations` | Step 5 | max_iterations 超過 | `termination_reason: human_escalation` |
| `superseded_by_decision` | Step 0 / Step 5 | anchor comment で別 Issue が決定 | `termination_reason: superseded_by_decision` |

### Human Escalation Classification（人間エスカレーション分類）

| Class | 条件 | 期待する人間アクション |
|---|---|---|
| A: 即時停止 | `state/needs-human` 存在 / `fail_closed.required` | 本文修正 → ラベル除去 → 再実行 |
| B: scope change | scope signal かつ anchor reframe なし | scope を縮小 or 分割 Issue 起票 |
| C: human_escalation | `max_iterations` 到達 | `BLOCKER summary` と `termination_reason` を付けて人間判断へ渡す |
| D: superseded | anchor comment で別決定 | 代替 Issue を確認・クローズ判断 |

## Observability（可観測性）

### Loop State の人間可読出力

ループ終了後、orchestrator は Issue コメントに `LOOP_STATE` YAML を投稿する。コメントフォーマット:

```
## issue-refinement-loop: 完了 (<timestamp>)

- termination_reason: <値>
- iteration: <値>
- last_verdict: <値>
- improvements_applied: <値>
```

### Audit Trail（監査証跡）

- `blockers_history[]`: 各イテレーションで検出した blocker
- `improvements_applied[]`: 適用した改善の記録
- `scope_signal_guard.triggered` / `reason_code`: scope 変更の検出記録
- `anchor_comment.*`: anchor comment の分類・検証結果

## DERIVED_RUNTIME_CLAIMS_V1

```yaml
claims:
  max_iterations_default:
    canonical_source: .claude/skills/issue-refinement-loop/SKILL.md
    canonical_selector: loop_policy.default_max_iterations
    expected_value: 3
  iteration_limit_termination:
    canonical_source: .claude/skills/issue-refinement-loop/SKILL.md
    canonical_selector: needs-fix continuation rule
    expected_value: iteration + 1 >= max_iterations -> human_escalation
  review_result_contract:
    canonical_source: .claude/skills/issue-refinement-loop/SKILL.md
    canonical_selector: Step 2 result contract
    expected_value: ISSUE_REVIEW_RESULT_COMPACT_V1
  loop_state_reference:
    canonical_source: .claude/skills/issue-refinement-loop/SKILL.md
    canonical_selector: LOOP_STATE
    expected_value: references/loop-state.md
```

## Verification Plan（検証計画）

本設計書の AC（derived_design_note として）:

| VC | 期待結果 |
|---|---|
| `test -f docs/dev/workflows/issue-refinement-loop-design.md` | PASS |
| `rg -n "^## Authority Map$" docs/dev/workflows/issue-refinement-loop-design.md` | 行番号ヒット |
| `rg -n "design_doc_schema_version\|ssot_classification\|canonical_sources\|conflict_rule\|loaded_when\|not_loaded_when\|summary_budget" docs/dev/workflows/issue-refinement-loop-design.md` | 全キー検出 |
| `rg -n "Producer.*Consumer.*Schema\|Fields read by orchestrator\|Opaque forwarded fields" docs/dev/workflows/issue-refinement-loop-design.md` | ヒット |
| `rg -n "\.claude/agents/\|\.claude/skills/" docs/dev/workflows/issue-refinement-loop-design.md` | 参照リンクヒット |

## Change Management（変更管理）

本設計書を変更する際のルール:

1. `canonical_sources` に列挙された正本と矛盾する変更を加えない（`conflict_rule: canonical_sources_win`）
2. SubAgent Contract Matrix の変更は、対応する `.claude/agents/*.md` / SKILL.md の変更と同一 PR で行う
3. 設計判断の変更は ADR 追加または既存 ADR 更新を検討する
4. `loaded_when` / `not_loaded_when` の条件変更は `docs/dev/ssot-registry.md` のエントリも同期更新する

## Authority Map（権限マップ）

| Topic | Canonical source | This doc may do | This doc must not do |
|---|---|---|---|
| Loop procedure（手順） | `.claude/skills/issue-refinement-loop/SKILL.md` | 設計判断を要約・参照リンク追加 | 手順を全文複製・上書き |
| SubAgent 役割・permissionMode | `.claude/agents/*.md` | Contract Matrix で role/schema を記録 | agent 定義を全文複製 |
| LOOP_STATE フィールド定義 | `.claude/skills/issue-refinement-loop/SKILL.md` | フィールド所有者と変更ルールを記録 | フィールド定義を複製（drift を招く） |
| planner ロジック | `plan_refinement_loop.py` | planner が produce する schema 名を参照 | ロジックを再実装・複製 |
| 開発フロー全体 | `docs/dev/workflow.md` | 本ループの位置づけを参照リンクで示す | フロー全体を再記述 |
| SubAgent 責務境界 | `docs/dev/agent-skill-boundaries.md` | 境界ルールへの参照リンク | 境界ルールを再定義 |
| anchor comment handling | `.claude/skills/issue-refinement-loop/references/anchor-comment-handling.md` | 正規化ルールの設計根拠を記録 | raw handling ロジックを複製 |

## Static execution decision projection

`ISSUE_EXECUTION_DECISION_V1` is a static contract for the planning producer → schema validation →
state/handoff projection → consumer route path. Its canonical policy is `docs/dev/workflow.md#execution-planning-policy-canonical-ssot`;
the schema is `.claude/skills/issue-refinement-loop/schemas/issue_execution_decision_v1.schema.json`.
This derived design note does not change runtime state/handoff or planner consumers. The migration projection is
`dual-write` → `dual-read` → `equivalence` → `new-authoritative` → `old removal`, with existing open-pr hard gates retained.

## Traceability（追跡可能性）

実装 Issue および関連 Issue:
- #392: issue-refinement-loop の deterministic planner/checker script 抽出
- #393: issue-refinement-loop SKILL.md を thin entrypoint + references 構成へ分割
- #394: SubAgent 固有契約の SubAgent 側への移管
- #384: 関連ループ改善
- #385: 関連ループ改善
- #387: 関連ループ改善
- #410: baseline_vc_preflight classifier 拡張
- #422: 本設計書の作成 Issue

> Issue / PR の現在状態は GitHub 側を正本とし、本設計書では複製しない。

## 実体参照

本設計書に記載した手順・定義の実体は以下を参照すること（DRY 遵守）:

- `.claude/skills/issue-refinement-loop/SKILL.md` — orchestrator の手順・LOOP_STATE 定義・Guardrails
- `.claude/skills/issue-refinement-loop/references/` — anchor comment 処理・scope guard・termination policy 等の詳細
- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` — planner ロジックの実体
- `.claude/agents/issue-author.md` — issue-author SubAgent 定義
- `.claude/agents/issue-reviewer.md` — issue-reviewer SubAgent 定義
- `.claude/agents/codebase-investigator.md` — codebase-investigator SubAgent 定義
- `.claude/agents/web-researcher.md` — web-researcher SubAgent 定義
