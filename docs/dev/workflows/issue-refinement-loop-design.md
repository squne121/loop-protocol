---
design_doc_schema_version: v1
ssot_classification: derived_design_note
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

## Status

| フィールド | 値 |
|---|---|
| 設計書バージョン | v1 |
| 対象スキル | `issue-refinement-loop` |
| 最終更新 Issue | #422 |
| 状態 | active |

## Purpose

Issue 本文の品質を反復改善する `issue-refinement-loop` の設計判断・責務境界・停止条件・人間エスカレーション分類を、正本への参照リンクと共に記録する。

本設計書が説明する主要な設計判断:

- planner が判定の正本であり、orchestrator は prose 再判定しない
- web research 結果は orchestrator が opaque に扱う
- anchor comment の raw snapshot を writer に直接流さない
- scope change signal で停止する条件
- follow-up materialization の dedupe key 設計
- `state/blocked` / `state/queued` hygiene の範囲
- `max_iterations=1` のデフォルト根拠
- human escalation 分類表

## Non-goals

- `plan_refinement_loop.py` のロジック詳細（planner の SSOT はスクリプト本体）
- `web-researcher` / `gemini-cli-headless-delegation` の retry/fallback 設計詳細
- `.claude/agents/*.md` の責務移動
- SubAgent 定義全文の複製（SSOT 分裂を招く）
- SKILL.md 手順全文の複製

## Invocation Contract

| フィールド | 内容 |
|---|---|
| 呼び出しトリガー | 「Issue ◯◯ を改善して」「refinement loop」 |
| 必須入力 | `issue_number` |
| 任意入力 | `max_iterations`（既定 1）、`anchor_comment_url` |
| 出力 | `LOOP_STATE` YAML（termination_reason 含む） |
| 前提条件 | `state/needs-human` / `state/done` 非存在 |
| 完了後 | `issue-contract-review` による go 判定後に `impl-review-loop` へ引き継ぐ |

### 入力スキーマ参照

正本: `.claude/skills/issue-refinement-loop/SKILL.md` の `## Inputs` セクション

## Workflow Topology

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
   ├─ iteration + 1 >= max_iterations → Step 5 (needs_second_pass)
   └─ else → Step 4 (issue-author) → next iteration
```

設計判断: Step 3（adversarial review）と Step 1.5（spec document review）は採用しない。Step 番号は履歴互換のため保持する。

### Topology 設計根拠

1. **planner-first**: `REFINEMENT_LOOP_PLAN_V1` が investigation/web_research/scope_signal の要否を決定する。orchestrator は planner 出力を consume するだけで prose 再判定しない（split-brain 防止）。
2. **max_iterations=1 の既定**: refinement loop は「1 周で十分な品質へ引き上げる」設計。超過時は `needs_second_pass` として人間に渡す。繰り返し改善は human-in-the-loop で管理する。
3. **anchor comment の正規化**: raw snapshot を reviewer/writer に直接流すと hallucination リスクが高まるため、orchestrator が正規化済み `anchor_comment_feedback` のみを渡す。

## State Model

`LOOP_STATE` の完全フィールド定義は `.claude/skills/issue-refinement-loop/SKILL.md` の `## LOOP_STATE Summary` セクションを参照する。

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

### State transition rules

- `scope_signal_guard.triggered == true` かつ `excluded_by_anchor_reframe == false` → `termination_reason: human_escalation` で即停止
- `fail_closed.required == true`（planner 出力）→ 即停止
- `iteration >= max_iterations` → `termination_reason: needs_second_pass`
- `last_verdict == approve` → `termination_reason: approved`

## SubAgent Contract Matrix

| SubAgent | Role | Producer | Consumer | Schema/version | Fields read by orchestrator | Opaque forwarded fields | Must-not-read fields | Mutation owner | Failure class | Verification fixture |
|---|---|---|---|---|---|---|---|---|---|---|
| `codebase-investigator` | リポジトリ内エビデンス収集 | codebase-investigator | orchestrator | `REPO_EVIDENCE_REF_V1` | `final_classification`, `verified_claims`, `unresolved_claims` | `raw_evidence[]` | `anchor_comment.raw_snapshot` | read-only（mutation 禁止） | `investigation_failed` | `.claude/skills/issue-refinement-loop/tests/` |
| `web-researcher` | 外部仕様調査 | web-researcher | orchestrator | `WEB_RESEARCH_RESULT_V1` | `status`, `failure_class`, `verification_route`, `claims`, `unresolved_risks` | `result`（raw は opaque） | `retry_log`, `attempt_log`, `fallback_state` | read-only（mutation 禁止） | `web_research_failed` | `.claude/skills/issue-refinement-loop/tests/` |
| `issue-reviewer` | Issue レビュー判定 | issue-reviewer | orchestrator | `REVIEW_ISSUE_RESULT_V1` | `status`, `verdict`, `needs_second_pass`, `blocking_issues`, `non_blocking_improvements`, `diff_proposal` | なし | domain judgment の再解釈禁止 | read-only（mutation 禁止） | `review_failed` | `.claude/skills/review-issue/` |
| `issue-author` | Issue 本文更新 | orchestrator | issue-author | `anchor_comment_feedback`（正規化済み） | `update_status`, `edit_result` | なし | `anchor_comment.raw_snapshot`（直接渡し禁止） | write（Issue body のみ） | `edit_failed` | `.claude/skills/edit-issue/` |

### SubAgent 設計上の注意

- orchestrator は `WEB_RESEARCH_RESULT_V1` の `retry/fallback/attempt_log` を読んではならない（`#394` の責務越境防止）
- `codebase-investigator` は `final_classification` の確定責務を持たない（orchestrator が main thread で確定）
- `issue-author` への入力は必ず `anchor_comment_feedback`（正規化済み）を使い、raw snapshot を直接渡してはならない

## Artifact and Evidence Contract

| Artifact | 生成者 | 保存先 | 形式 | 消費者 |
|---|---|---|---|---|
| `REFINEMENT_LOOP_PLAN_V1` | `plan_refinement_loop.py` | stdout（memory only） | JSON | orchestrator Step 0f |
| `REVIEW_ISSUE_RESULT_V1` | `issue-reviewer` | stdout（memory only） | YAML | orchestrator Step 2 |
| `LOOP_STATE` (final) | orchestrator | Issue コメント | YAML | human / impl-review-loop |
| anchor comment snapshot | orchestrator (Step 0) | `LOOP_STATE.anchor_comment` | 正規化 YAML | issue-author（正規化済みのみ） |

`decision: not_applicable`（docs-only 変更等）の場合は artifact 出力不要。

## Context Loading Policy

本設計書は以下の場面でのみロードする（`loaded_when` / `not_loaded_when` 参照）:

| シナリオ | ロード可否 | 理由 |
|---|---|---|
| architecture review | YES | 設計判断の参照が必要 |
| contract migration | YES | SubAgent Contract の変更影響確認 |
| failure-mode update | YES | 停止条件・escalation 分類の参照 |
| normal loop execution | NO | runtime overhead なし。正本 SKILL.md を参照 |
| routine issue refinement | NO | 派生ノートのロードは不要 |

### Progressive Disclosure

本設計書の全文ロードが不要な場合の参照手順:

1. SubAgent 責務境界のみ → `## SubAgent Contract Matrix` のみ読む
2. 停止条件のみ → `## Failure Modes and Recovery` を読む
3. escalation 分類のみ → `## Failure Modes and Recovery` の Human Escalation Classification を読む

## Guardrails

1. **planner SSOT 遵守**: orchestrator は `REFINEMENT_LOOP_PLAN_V1` の `decisions.*` を prose 再判定しない。planner が `required: false` と返した investigation/web_research は実行しない。
2. **anchor comment 正規化**: raw snapshot を SubAgent への入力として直接使用しない。必ず orchestrator が `anchor_comment_feedback` に正規化してから渡す。
3. **WEB_RESEARCH_RESULT_V1 の opaque 扱い**: retry/fallback フィールドを読まない。`status` / `failure_class` / `verification_route` / `claims` / `unresolved_risks` のみ orchestrator が読む。
4. **state/blocked hygiene の範囲**: `state/blocked` / `state/queued` の除去は「本 Issue への refinement 継続が確定した後」にのみ行う（Step 0 完了後）。
5. **fail-close**: `fail_closed.required == true` の場合は即停止し、人間判断を仰ぐ。

## Failure Modes and Recovery

### Failure Mode 分類表

| Failure Mode | 検出タイミング | 停止条件 | Recovery アクション |
|---|---|---|---|
| `state/needs-human` 存在 | Step 0 | Hard stop | 人間判断。ラベル除去まで待機 |
| `fail_closed.required == true` | Step 0f | Hard stop | 人間判断へ escalate |
| scope change signal | Step 2 / Step 4 | `scope_signal_guard.triggered == true` かつ anchor reframe なし | `termination_reason: human_escalation` |
| investigation failure | Step 1 | `failure_class: investigation_failed` | 人間判断へ escalate（partial evidence で進めない） |
| web research failure | Step 1b | `failure_class: web_research_failed` | `skip_reason` に理由を記録し、web research なしで続行（critical claim がある場合は停止） |
| `iteration >= max_iterations` | Step 5 | max_iterations 超過 | `termination_reason: needs_second_pass`。人間が second pass を判断 |
| `superseded_by_decision` | Step 0 / Step 5 | anchor comment で別 Issue が決定 | `termination_reason: superseded_by_decision` |

### Human Escalation Classification

| Class | 条件 | 期待する人間アクション |
|---|---|---|
| A: 即時停止 | `state/needs-human` 存在 / `fail_closed.required` | 本文修正 → ラベル除去 → 再実行 |
| B: scope change | scope signal かつ anchor reframe なし | scope を縮小 or 分割 Issue 起票 |
| C: needs_second_pass | `max_iterations` 到達 | 品質確認後、再実行またはそのまま着手 |
| D: superseded | anchor comment で別決定 | 代替 Issue を確認・クローズ判断 |

## Observability

### Loop State の人間可読出力

ループ終了後、orchestrator は Issue コメントに `LOOP_STATE` YAML を投稿する。コメントフォーマット:

```
## issue-refinement-loop: 完了 (<timestamp>)

- termination_reason: <値>
- iteration: <値>
- last_verdict: <値>
- improvements_applied: <値>
```

### Audit Trail

- `blockers_history[]`: 各イテレーションで検出した blocker
- `improvements_applied[]`: 適用した改善の記録
- `scope_signal_guard.triggered` / `reason_code`: scope 変更の検出記録
- `anchor_comment.*`: anchor comment の分類・検証結果

## Verification Plan

本設計書の AC（derived_design_note として）:

| VC | 期待結果 |
|---|---|
| `test -f docs/dev/workflows/issue-refinement-loop-design.md` | PASS |
| `rg -n "^## Authority Map$" docs/dev/workflows/issue-refinement-loop-design.md` | 行番号ヒット |
| `rg -n "design_doc_schema_version\|ssot_classification\|canonical_sources\|conflict_rule\|loaded_when\|not_loaded_when\|summary_budget" docs/dev/workflows/issue-refinement-loop-design.md` | 全キー検出 |
| `rg -n "Producer.*Consumer.*Schema\|Fields read by orchestrator\|Opaque forwarded fields" docs/dev/workflows/issue-refinement-loop-design.md` | ヒット |
| `rg -n "\.claude/agents/\|\.claude/skills/" docs/dev/workflows/issue-refinement-loop-design.md` | 参照リンクヒット |

## Change Management

本設計書を変更する際のルール:

1. `canonical_sources` に列挙された正本と矛盾する変更を加えない（`conflict_rule: canonical_sources_win`）
2. SubAgent Contract Matrix の変更は、対応する `.claude/agents/*.md` / SKILL.md の変更と同一 PR で行う
3. 設計判断の変更は ADR 追加または既存 ADR 更新を検討する
4. `loaded_when` / `not_loaded_when` の条件変更は `docs/dev/ssot-registry.md` のエントリも同期更新する

## Authority Map

| Topic | Canonical source | This doc may do | This doc must not do |
|---|---|---|---|
| Loop procedure（手順） | `.claude/skills/issue-refinement-loop/SKILL.md` | 設計判断を要約・参照リンク追加 | 手順を全文複製・上書き |
| SubAgent 役割・permissionMode | `.claude/agents/*.md` | Contract Matrix で role/schema を記録 | agent 定義を全文複製 |
| LOOP_STATE フィールド定義 | `.claude/skills/issue-refinement-loop/SKILL.md` | フィールド所有者と変更ルールを記録 | フィールド定義を複製（drift を招く） |
| planner ロジック | `plan_refinement_loop.py` | planner が produce する schema 名を参照 | ロジックを再実装・複製 |
| 開発フロー全体 | `docs/dev/workflow.md` | 本ループの位置づけを参照リンクで示す | フロー全体を再記述 |
| SubAgent 責務境界 | `docs/dev/agent-skill-boundaries.md` | 境界ルールへの参照リンク | 境界ルールを再定義 |
| anchor comment handling | `.claude/skills/issue-refinement-loop/references/anchor-comment-handling.md` | 正規化ルールの設計根拠を記録 | raw handling ロジックを複製 |

## References

実装 Issue および関連 Issue:
- #392 CLOSED: issue-refinement-loop の deterministic planner/checker script 抽出
- #393 CLOSED: issue-refinement-loop SKILL.md を thin entrypoint + references 構成へ分割
- #394 CLOSED: SubAgent 固有契約の SubAgent 側への移管
- #384 CLOSED: 関連ループ改善
- #385 OPEN: 関連ループ改善
- #387 OPEN: 関連ループ改善
- #410 CLOSED: baseline_vc_preflight classifier 拡張
- #422 CLOSED: 本設計書の作成 Issue

## 実体参照

本設計書に記載した手順・定義の実体は以下を参照すること（DRY 遵守）:

- `.claude/skills/issue-refinement-loop/SKILL.md` — orchestrator の手順・LOOP_STATE 定義・Guardrails
- `.claude/skills/issue-refinement-loop/references/` — anchor comment 処理・scope guard・termination policy 等の詳細
- `.claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py` — planner ロジックの実体
- `.claude/agents/issue-author.md` — issue-author SubAgent 定義
- `.claude/agents/issue-reviewer.md` — issue-reviewer SubAgent 定義
- `.claude/agents/codebase-investigator.md` — codebase-investigator SubAgent 定義
- `.claude/agents/web-researcher.md` — web-researcher SubAgent 定義
