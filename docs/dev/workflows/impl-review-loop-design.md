---
design_doc_schema_version: v1
ssot_classification: derived_design_note
canonical_sources:
  - docs/dev/workflow.md
  - docs/dev/agent-skill-boundaries.md
  - .claude/skills/impl-review-loop/SKILL.md
  - .claude/agents/implementation-worker.md
  - .claude/agents/test-runner.md
  - .claude/agents/pr-reviewer.md
conflict_rule: canonical_sources_win
loaded_when:
  - architecture review of impl-review-loop
  - contract migration affecting LOOP_STATE or SubAgent interfaces
  - failure-mode update for implementation loop
  - PR conflict / escalation runbook review
not_loaded_when:
  - normal loop execution (runtime)
  - routine implementation work
  - any SubAgent execution within the loop itself
summary_budget: "<= 1200 chars"
---

# impl-review-loop 詳細設計書

> **derived_design_note**: 本文書は `canonical_sources` に列挙された正本を補足する派生ノートである。
> 本文書と正本が矛盾する場合、常に正本が勝つ（`conflict_rule: canonical_sources_win`）。

## Status

| フィールド | 値 |
|---|---|
| 設計書バージョン | v1 |
| 対象スキル | `impl-review-loop` |
| 最終更新 Issue | #422 |
| 状態 | active |

## Purpose

`impl-review-loop` の control-plane / data-plane 境界・`LOOP_STATE` フィールド所有権・PR conflict 時の escalation 方針・`CONTRACT_REVIEW_RESULT_V1` を作業計画正本として扱う根拠を、正本への参照リンクと共に記録する。

本設計書が説明する主要な設計判断:

- control-plane / data-plane 境界（orchestrator が操作してよいものとダメなものの境界）
- `CONTRACT_REVIEW_RESULT_V1` を作業計画正本として扱う理由
- `LOOP_STATE` の field owner と mutation ルール
- `product_spec_preflight` の routing table 設計
- `external_research_skip_basis` の記録規則
- PR conflict / dirty / blocked 時の escalation 方針
- `max_iterations=5` のデフォルト根拠

## Non-goals

- `implement-issue` SKILL.md の手順詳細（正本は SKILL.md）
- `pr-review-judge` の判定ロジック詳細（正本は SKILL.md）
- SubAgent 定義全文の複製（SSOT 分裂を招く）
- SKILL.md 手順全文の複製

## Invocation Contract

| フィールド | 内容 |
|---|---|
| 呼び出しトリガー | `/impl-review-loop <N>` / 「Issue ◯◯ をループで実装して」 |
| 必須入力 | `issue_number` |
| 任意入力 | `contract_snapshot_url`（省略時は preparation で自動取得）、`max_iterations`（既定 5） |
| 前提条件 | `issue-contract-review` が `status: go` を返していること、`state/needs-human` 非存在 |
| 完了条件 | `LOOP_VERDICT: APPROVE`（PR は人間がマージ判断） |

### 着手条件の設計根拠

`CONTRACT_REVIEW_RESULT_V1 status: go` を作業計画正本として扱う根拠:
- DoR（Definition of Ready）準拠確認・VC preflight・dependency close・human escalation 非該当がすべて `issue-contract-review` で検証済み
- 二重確認による手順冗長化を防ぐため、impl-review-loop 側では contract 内容を再判定しない
- 正本: `docs/dev/workflow.md` の `## Issue contract を作業計画の正本として扱う条件`

## Workflow Topology

```
[Preparation: state 初期化・worktree 確認・contract_snapshot 取得]
       ↓
[Step 1: Implementation]  → implementation-worker SubAgent (implement-issue skill)
       ↓
[Step 2: Verification]    → test-runner SubAgent
       ↓
[Step 4: PR Review]       → pr-reviewer SubAgent (pr-review-judge skill)
       ↓
[Step 5: Judgment]        → LOOP_VERDICT 解析
       ↓
   APPROVE → 終了（PR は人間がマージ判断）
   REQUEST_CHANGES → fix_delta を Step 1 に渡して次 iteration
   上限超過 → fail-close → 人間判断
```

設計判断: Step 3（adversarial review）と Step 1.5（spec document review）は採用しない。Step 番号は履歴互換のため 1 → 2 → 4 → 5 のまま保持する。

### Control-plane / Data-plane 境界

**Control-plane（orchestrator が担う）**:
- LOOP_STATE の読み書き
- SubAgent への委譲判断と routing
- iteration カウンタ管理
- termination condition の評価

**Data-plane（SubAgent に委譲）**:
- git push / `gh pr create` / `gh pr edit`（open-pr skill 経由）
- 実際のファイル編集・実装（implementation-worker）
- pnpm コマンド実行・テスト実行（test-runner）
- `gh pr review` による GitHub verdict 記録（pr-reviewer）

orchestrator は data-plane 操作を直接行わない。

### max_iterations=5 の設計根拠

- 3 イテレーション以内で APPROVE に至るケースが大半
- 5 を上限とすることで「無限ループ防止」と「genuine な fix cycle 許容」を両立
- 5 超過は「根本的な contract の問題」として人間判断へ委ねる

> `max_iterations=5` は運用上の安全上限値であり、実測に基づく最適値ではない。無限の review/implementation ループを防ぐための暫定キャップである。運用ログから適切なしきい値が判明した場合は、ワークフロー変更プロセスを通じて値を更新すること。

## State Model

`LOOP_STATE` の完全フィールド定義は `.claude/skills/impl-review-loop/SKILL.md` の `## LOOP_STATE YAML` セクションを参照する。

以下はフィールドの所有者と変更ルールのサマリ:

| フィールド グループ | Owner | 変更タイミング |
|---|---|---|
| `issue_number` / `contract_snapshot_url` | orchestrator (preparation) | 初期化時に確定、変更禁止 |
| `iteration` / `last_step` | orchestrator | 各 Step 完了後 |
| `last_loop_verdict` | orchestrator (Step 5) | LOOP_VERDICT 受信時 |
| `worktree` / `branch` | orchestrator (preparation) | preparation で確定 |
| `blockers_history` | orchestrator (Step 5) | REQUEST_CHANGES 時に追記 |
| `external_research_skip_basis` | orchestrator | スキップ判断時に記録（null 不可） |
| `termination_reason` | orchestrator (Step 5) | 終了時に確定 |
| `product_spec_preflight` | orchestrator (preparation / Step 1 前) | contract snapshot から正規化・格納 |

### product_spec_preflight routing table

正本: `.claude/skills/impl-review-loop/SKILL.md` の `## Product Spec Check Reference`

| 状態 | routing_action |
|---|---|
| `checks.product_spec_check` が snapshot に存在しない | `refresh_contract_snapshot` |
| `applicability == not_applicable && decision == pass` | `continue` |
| `applicability == not_applicable && decision != pass` | `refresh_contract_snapshot` |
| `decision == fail` | `stop_human` |
| `decision == human_judgment` | `stop_human` |
| `decision == pass && applicability == applicable` | `continue` |
| 不正な enum 値 | `refresh_contract_snapshot` |

### external_research_skip_basis の記録規則

- LOOP_PROTOCOL は internal-only 変更が多い前提のため、デフォルトはスキップ
- スキップ時も判定根拠を `external_research_skip_basis` に記録（null 不可）
- 外部仕様調査が必要な場合は `gemini-cli-headless-delegation` skill を使い、結果を記録

## SubAgent Contract Matrix

| SubAgent | Role | Producer | Consumer | Schema/version | Fields read by orchestrator | Opaque forwarded fields | Must-not-read fields | Mutation owner | Failure class | Verification fixture |
|---|---|---|---|---|---|---|---|---|---|---|
| `implementation-worker` | Issue 実装・worktree 管理・PR 起票 | orchestrator（contract_snapshot_url, fix_delta） | orchestrator | `IMPLEMENT_RESULT_V1` | `status`, `pr_url`, `worktree`, `branch`, `verification.*`, `allowed_paths_compliance` | なし | data-plane の実装詳細（orchestrator は読まない） | write（ファイル・git・PR） | `implementation_failed` | `.claude/skills/implement-issue/` |
| `test-runner` | Verification Commands 実行 | orchestrator（worktree, branch） | orchestrator | `TEST_RESULT_V1` | `status`, `passed`, `failed`, `details[]` | なし | test 実行の内部 stderr 詳細（summary のみ） | read-only（mutation 禁止） | `verification_failed` | `.claude/agents/test-runner.md` |
| `pr-reviewer` | PR コードレビュー・LOOP_VERDICT 記録 | orchestrator（pr_url） | orchestrator | `LOOP_VERDICT: APPROVE | REQUEST_CHANGES` | `verdict`, `blockers[]`, `reviewed_head_sha`, `follow_up_issue_requests` | なし | review 判断の内部 rationale（verdict のみ読む） | write（`gh pr review` のみ） | `pr_review_failed` | `.claude/skills/pr-review-judge/` |

### SubAgent 設計上の注意

- orchestrator は `IMPLEMENT_RESULT_V1` の verification 詳細をパースするが、実装の内部判断を re-evaluate しない
- `pr-reviewer` の `verdict: REQUEST_CHANGES` には `blockers[]` が含まれ、orchestrator はこれを `fix_delta` として次 iteration の Step 1 に渡す
- `test-runner` は Verification Commands の実行専用。impl-review-loop が `baseline_vc_preflight.py` を重複実行しない

## Artifact and Evidence Contract

| Artifact | 生成者 | 保存先 | 形式 | 消費者 |
|---|---|---|---|---|
| `IMPLEMENT_RESULT_V1` | implementation-worker | stdout（memory） | YAML | orchestrator Step 1 後 |
| `TEST_RESULT_V1` | test-runner | stdout（memory） | YAML | orchestrator Step 2 後 |
| `LOOP_VERDICT` | pr-reviewer | GitHub PR review + stdout | KEY=VALUE | orchestrator Step 5 |
| `LOOP_STATE` (final) | orchestrator | Issue コメント | YAML | human / post-merge-cleanup |
| PR | implementation-worker (open-pr skill) | GitHub | Pull Request | 人間レビュー |

`decision: not_applicable`（Runtime Verification Applicability）の場合は `artifacts/` 出力不要。`decision: immediate` の場合は証跡を PR 本文に添付する。

## Context Loading Policy

本設計書は以下の場面でのみロードする（`loaded_when` / `not_loaded_when` 参照）:

| シナリオ | ロード可否 | 理由 |
|---|---|---|
| architecture review | YES | 設計判断の参照が必要 |
| contract migration | YES | LOOP_STATE / SubAgent interface 変更影響確認 |
| failure-mode update | YES | escalation 分類の参照 |
| PR conflict runbook review | YES | CONFLICTING PR Escalation Runbook 参照 |
| normal loop execution | NO | runtime overhead なし。正本 SKILL.md を参照 |
| routine implementation | NO | 派生ノートのロードは不要 |

### Progressive Disclosure

本設計書の全文ロードが不要な場合の参照手順:

1. SubAgent 責務境界のみ → `## SubAgent Contract Matrix` のみ読む
2. 停止条件のみ → `## Failure Modes and Recovery` を読む
3. state field owner のみ → `## State Model` の表を読む
4. routing table のみ → `## State Model` の `product_spec_preflight routing table` を読む

## Guardrails

1. **control-plane 専用**: orchestrator は data-plane 操作（git push / `gh pr create` / マージ等）を直接行わない
2. **LOOP_STATE イテレーション管理**: 各イテレーション完了後に LOOP_STATE を更新し、人間がループの全履歴を読めるようにする
3. **fail-close**: `max_iterations` 超過時は必ず `termination_reason: max_iterations` で停止
4. **構造化出力の遵守**: 全 SubAgent 出力は構造化フォーマット（YAML / KEY=VALUE）で受け取り、散文サマリで上書きしない
5. **adversarial review 不採用**: `LOOP_VERDICT` 判定は pr-review-judge の APPROVE 一本で完結（Step 3 採用しない）
6. **external_research_skip_basis 記録必須**: スキップ時も null にしない

## Failure Modes and Recovery

### Failure Mode 分類表

| Failure Mode | 検出タイミング | 停止条件 | Recovery アクション |
|---|---|---|---|
| `state/needs-human` 存在 | Preparation | Hard stop | ラベル除去まで待機 |
| `contract_snapshot` 取得失敗 | Preparation | Hard stop | `issue-contract-review` 再実行 |
| `product_spec_preflight: stop_human` | Preparation / Step 1 前 | Hard stop | 人間が product spec を確認 |
| `product_spec_preflight: refresh_contract_snapshot` | Preparation | loop stop | `issue-contract-review` 再実行 |
| implementation failed | Step 1 | `IMPLEMENT_RESULT_V1.status: failed` | fix_delta を確認し人間判断 |
| verification failed | Step 2 | `TEST_RESULT_V1.status: failed` | implementation-worker に修正を委譲 |
| PR conflict / dirty | Step 4 | `merge_state_status: CONFLICTING / DIRTY / BLOCKED` | CONFLICTING PR Escalation Runbook 参照 |
| `iteration >= max_iterations` | Step 5 | max_iterations 超過 | `termination_reason: max_iterations`。人間判断 |
| `human_review_required: true` | Step 1-4 のいずれか | 即停止 | 人間判断を仰ぐ |

### CONFLICTING PR 処理方針

詳細は `.claude/skills/impl-review-loop/steps/conflicting-pr-escalation-runbook.md` を参照する。

概要:
- `merge_state_status: CONFLICTING` が継続する場合は escalation runbook へ
- orchestrator が直接 `git rebase` / force push を行うことは禁止
- rebase 作業は実装担当の SubAgent に委譲する

### Human Escalation Classification

| Class | 条件 | 期待する人間アクション |
|---|---|---|
| A: 即時停止 | `state/needs-human` / contract 未取得 | contract 整備後に再実行 |
| B: product_spec stop | `product_spec_preflight: stop_human` | product spec 確認 |
| C: max_iterations | iteration 上限到達 | 実装内容確認・root cause 調査 |
| D: human_review_required | SubAgent からの escalation | PR / Issue の内容確認 |
| E: PR conflict | `CONFLICTING / DIRTY / BLOCKED` 継続 | コンフリクト解消 |

## Observability

### Loop State の人間可読出力

ループ終了後、orchestrator は Issue コメントに `LOOP_STATE` YAML を投稿する。

### Audit Trail

- `blockers_history[]`: REQUEST_CHANGES の各回で記録した blocker
- `external_research_skip_basis`: 外部調査スキップの根拠（null 禁止）
- `termination_reason`: 終了理由（approved / max_iterations / human_escalation）
- `product_spec_preflight.*`: product spec check の routing 結果

## Verification Plan

本設計書の AC（derived_design_note として）:

| VC | 期待結果 |
|---|---|
| `test -f docs/dev/workflows/impl-review-loop-design.md` | PASS |
| `rg -n "^## Authority Map$" docs/dev/workflows/impl-review-loop-design.md` | 行番号ヒット |
| `rg -n "design_doc_schema_version\|ssot_classification\|canonical_sources\|conflict_rule\|loaded_when\|not_loaded_when\|summary_budget" docs/dev/workflows/impl-review-loop-design.md` | 全キー検出 |
| `rg -n "Producer.*Consumer.*Schema\|Fields read by orchestrator\|Opaque forwarded fields" docs/dev/workflows/impl-review-loop-design.md` | ヒット |
| `rg -n "\.claude/agents/\|\.claude/skills/" docs/dev/workflows/impl-review-loop-design.md` | 参照リンクヒット |

## Change Management

本設計書を変更する際のルール:

1. `canonical_sources` に列挙された正本と矛盾する変更を加えない（`conflict_rule: canonical_sources_win`）
2. SubAgent Contract Matrix の変更は、対応する `.claude/agents/*.md` / SKILL.md の変更と同一 PR で行う
3. LOOP_STATE フィールドの追加・削除は `.claude/skills/impl-review-loop/SKILL.md` を先に更新し、本設計書はその後に同期更新する
4. `loaded_when` / `not_loaded_when` の条件変更は `docs/dev/ssot-registry.md` のエントリも同期更新する

## Authority Map

| Topic | Canonical source | This doc may do | This doc must not do |
|---|---|---|---|
| Loop procedure（手順） | `.claude/skills/impl-review-loop/SKILL.md` | 設計判断を要約・参照リンク追加 | 手順を全文複製・上書き |
| SubAgent 役割・permissionMode | `.claude/agents/*.md` | Contract Matrix で role/schema を記録 | agent 定義を全文複製 |
| LOOP_STATE フィールド定義 | `.claude/skills/impl-review-loop/SKILL.md` | フィールド所有者と変更ルールを記録 | フィールド定義を複製（drift を招く） |
| 開発フロー全体 | `docs/dev/workflow.md` | 本ループの位置づけを参照リンクで示す | フロー全体を再記述 |
| SubAgent 責務境界 | `docs/dev/agent-skill-boundaries.md` | 境界ルールへの参照リンク | 境界ルールを再定義 |
| PR conflict / escalation | `.claude/skills/impl-review-loop/steps/conflicting-pr-escalation-runbook.md` | 概要と参照リンクを記録 | runbook 全文を複製 |
| product_spec_preflight 実装 | `.claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py` | routing table の設計意図を説明 | スクリプトロジックを複製 |
| Issue contract 着手条件 | `docs/dev/workflow.md` の `## Issue contract を作業計画の正本として扱う条件` | 設計根拠へのリンク | 条件を複製 |

## Traceability

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

- `.claude/skills/impl-review-loop/SKILL.md` — orchestrator の手順・LOOP_STATE 定義・Guardrails
- `.claude/skills/impl-review-loop/steps/` — 各 Step の詳細手順
- `.claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py` — product_spec_preflight 判定スクリプト
- `.claude/agents/implementation-worker.md` — implementation-worker SubAgent 定義
- `.claude/agents/test-runner.md` — test-runner SubAgent 定義
- `.claude/agents/pr-reviewer.md` — pr-reviewer SubAgent 定義
- `.claude/skills/implement-issue/SKILL.md` — Step 1 で使う実装手順
- `.claude/skills/pr-review-judge/SKILL.md` — Step 4 で使うレビュー判定手順
