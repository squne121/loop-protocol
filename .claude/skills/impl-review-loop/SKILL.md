---
name: impl-review-loop
description: >-
  implementation child issue を **実装→検証→PR レビュー** の 3 ステップループで自律完了させるオーケストレーター。
  Issue 番号を受け取り、pr-reviewer の LOOP_VERDICT が APPROVE になるまで反復する。
  `/impl-review-loop <N>` または「Issue ◯◯ をループで実装して」のトリガーで使う。
  着手前に `docs/dev/workflow.md` の「Issue contract を作業計画の正本として扱う条件」と
  `issue-contract-review` の `status: go` を確認する。
---

# Impl Review Loop

implementation child issue を **実装 → 検証 → PR レビュー** の 3 ステップループで自律完了させるオーケストレーター skill。各ステップを SubAgent に委譲し、メインの control-plane（state tracking + routing）に責務を限定する。

## Inputs

- `issue_number`（必須）: implementation child issue 番号
- `contract_snapshot_url`（任意、省略時は preparation ステップで検出）: `issue-contract-review` で `status: go` を返したコメントの URL。未提供の場合は preparation ステップが Issue コメントから `CONTRACT_REVIEW_RESULT_V1 status: go` コメントを自動検出する（存在すれば採用）。`status: go` コメントが見つからない場合は `intake_gate_failed: missing_contract_go` として停止し、人間に contract-review の実行を依頼する（fail-only gate — 自動実行・自動 materialization は行わない）
- `max_iterations`（任意、デフォルト 3）: 上限回数。超過時は fail-close で人間判断を仰ぐ

## Loop Structure

```
[Step 1: Implementation]  → implementation-worker SubAgent (implement-issue skill)
        ↓
[Step 2: Verification]    → test-runner SubAgent
        ↓
[Step 4: PR Review]       → pr-reviewer SubAgent (pr-review-judge skill)
        ↓
[Step 5: Judgment]        → LOOP_VERDICT_V2 を解析
        ↓
    APPROVE + merge_ready == true + required_auto_actions == [] → 終了（PR は人間がマージ判断）
    APPROVE + required_auto_actions 残あり → worker 委譲 → PR review 再実行
    APPROVE + merge_ready == false → mergeability handling（BEHIND 分岐等）
    REQUEST_CHANGES → Step 1 に戻る（fix_delta を渡す）
    上限超過 → 人間判断を仰ぐ
```

> Step 3（adversarial review）と Step 1.5（spec document review）は LOOP_PROTOCOL では採用しない（PR #12 / #20 方針）。Step 番号は履歴互換のため 1 → 2 → 4 → 5 のまま保持する。

## Procedure

各 Step の詳細は `steps/` 配下に分割。実行時は下記の順で読む:

1. [事前準備（state 初期化・worktree 確認）](steps/preparation.md)
2. [Step 1: Implementation](steps/step-1-implementation.md)
3. [Step 2: Verification](steps/step-2-verification.md)
4. [Step 4: PR Review](steps/step-4-pr-review.md)
5. [Step 5: 判定・終了・フィードバック循環](steps/step-5-feedback-and-termination.md)
6. [Step 5: LOOP_VERDICT 自動読み取り（mergeability handling）](steps/step-5-mergeability-handling.md)
7. [CONFLICTING PR Escalation Runbook](steps/conflicting-pr-escalation-runbook.md)
8. [Context Protocol / Guardrails](steps/context-protocol-and-guardrails.md)

## LOOP_STATE YAML（state tracking の正本）

ループ実行中は以下の構造で state を保持する。orchestrator がイテレーションごとに更新し、次のイテレーションへ持ち越す:

```yaml
LOOP_STATE:
  issue_number: <int>
  contract_snapshot_url: <URL>
  contract_snapshot_source: provided | detected_existing
  iteration: <int, 0-indexed>
  max_iterations: 3
  worktree: .claude/worktrees/issue-<番号>-<slug>
  branch: worktree-issue-<番号>-<slug>
  last_step: implementation | verification | pr_review | judgment
  last_loop_verdict: APPROVE | REQUEST_CHANGES | null
  blockers_history: []
  external_research_skip_basis: "<理由 or null>"
  termination_reason: null | approved | max_iterations | human_escalation | intake_gate_failed
  product_spec_preflight:
    source: contract_snapshot.checks.product_spec_check
    applicability: applicable | not_applicable | missing
    decision: pass | fail | human_judgment | missing
    blocked_rule_ids: []
    contract_snapshot_url: "<url>"
    body_sha256: "<sha256>"
    routing_action: continue | stop_human | refresh_contract_snapshot
```

## 終了条件

| 条件 | アクション |
|---|---|
| `LOOP_VERDICT_V2.verdict: APPROVE` かつ `merge_ready == true` かつ `required_auto_actions == []` | 終了。`IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready` かつ `merge_ready: true` を emit。PR は人間がマージ判断 |
| `LOOP_VERDICT_V2.verdict: APPROVE` かつ `required_auto_actions` が空でない | 終了しない。required_auto_actions を worker に委譲し、PR review を再実行する |
| `LOOP_VERDICT_V2.verdict: APPROVE` かつ `merge_ready == false` | 終了しない。`step-5-mergeability-handling.md` の routing に従う（BEHIND 分岐等） |
| `iteration ≥ max_iterations` | fail-close。`termination_reason: max_iterations` を LOOP_STATE に記録、人間判断を仰ぐ |
| Step 1-4 のいずれかで `human_review_required: true` を SubAgent が返した | 即停止、人間判断を仰ぐ |
| `merge_state_status: CONFLICTING / DIRTY / BLOCKED` の繰り返し | CONFLICTING PR Escalation Runbook 参照 |

> **重要**: `verdict: APPROVE` 単独では終了しない。`merge_ready == true` かつ `required_auto_actions == []` の両条件が必要。

## 外部仕様調査の取扱い

外部仕様調査が必要な場合は `gemini-cli-headless-delegation` skill を default 経路として使い、結果を LOOP_STATE の `external_research_skip_basis` に記録する。LOOP_PROTOCOL は internal-only 変更が多い前提のため、デフォルトはスキップで構わない（スキップ時も判定根拠を記録する）。

## Contract Snapshot 参照ルール

preparation step で取得した contract snapshot 内の以下の情報を Step 1-4 で参照する:

### VC Preflight Reference

`vc_preflight` JSON（`baseline_vc_preflight.py` が生成）を参照し、impl-review-loop 側で `baseline_vc_preflight.py` を重複実行しない。VC 分類の正本は contract snapshot の `vc_preflight.classifications[]` に従う。

### Product Spec Check Reference (Issue #333)

`checks.product_spec_check` を contract snapshot から読み取り、Step 1 delegation 前に `LOOP_STATE.product_spec_preflight` に正規化して格納する。以下のルールに従う:

> **注意**: `refresh_contract_snapshot` へ route する場合は **route only; no auto-run** — AI が `issue-contract-review` を自動実行してはならない。停止して人間に `issue-contract-review` の再実行を依頼する。

- `checks.product_spec_check` が snapshot に存在しない場合は stale / incomplete snapshot として `refresh_contract_snapshot` へ route する（route only; no auto-run — 停止して人間に `issue-contract-review` の再実行を依頼する）
- `applicability == not_applicable && decision == pass` の場合のみ、無関係 Issue として `continue` へ継続
- `applicability == not_applicable && decision != pass` は inconsistent snapshot として `refresh_contract_snapshot` へ route する（route only; no auto-run）
- `decision == fail` → fail-closed で停止、`routing_action: stop_human`
- `decision == human_judgment` → 人間判断へ escalate、`routing_action: stop_human`
- `decision == pass` かつ `applicability == applicable` → 続行、`routing_action: continue`
- 不正な enum 値 → stale / invalid snapshot として `refresh_contract_snapshot` へ route する（route only; no auto-run）

**実装例**: `.claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py` が mutation-free CLI として `PRODUCT_SPEC_GATE_DECISION_V1` を出力する（routing_action: continue | stop_human | refresh_contract_snapshot）。

## Guardrails

- loop policy（何回まで自動で回すか）と Claude Code permission mode（ツール呼び出しの承認方式）は直交する概念であり、loop policy の継続判断に `--permission-mode` / `permissions.defaultMode` / `--dangerously-skip-permissions` を参照しない
- control-plane だけを担い、data-plane 操作（push / `gh pr edit` / マージ等）は SubAgent に委譲する
- LOOP_STATE をイテレーションごとに更新し、人間がループの全履歴を読めるようにする
- `max_iterations` 超過時は必ず fail-close（無限ループ防止）
- adversarial review は採用しないため `LOOP_VERDICT` 判定は pr-review-judge の APPROVE 一本で完結
- 全 SubAgent 出力は構造化フォーマット（YAML / KEY=VALUE）で受け取り、散文サマリで上書きしない
- **issue-contract-review 自動実行しない（fail-only gate）**: `status: go` が存在しない場合に `issue-contract-review` を自動実行する旧設計（auto run deprecated）は #564 以降廃止。`missing_contract_go` 判定時は人間に `issue-contract-review` 実行を依頼して停止する（#564 整合）。

## ALLOWED_PATHS_GATE_RESULT_V1 routing（AC5）

Step 1 (Implementation) で worker が emit する `ALLOWED_PATHS_GATE_RESULT_V1` の status field を canonical に route し、raw diff comparison を blocking 判定根拠にしない:

- `ALLOWED_PATHS_GATE_RESULT_V1.status: ok` → 続行（Allowed Paths 内確認）
- `ALLOWED_PATHS_GATE_RESULT_V1.status: fail_closed` → Step 1 再実行（worker 側で逸脱を修正）
- `ALLOWED_PATHS_GATE_RESULT_V1.status: stale_snapshot` → human_escalation（contract snapshot 再確認が必要）
- `ALLOWED_PATHS_GATE_RESULT_V1.status: indeterminate` → human_escalation（機械可読 decision 確定不能）

**重要**: control-plane は raw git diff を参照して「このファイルは Allowed Paths 外では？」などの人間判断を入れない。judgment 根拠は exclusively `ALLOWED_PATHS_GATE_RESULT_V1.status` に限定する。worker の `IMPLEMENT_RESULT_V1.allowed_paths_compliance` は参考情報（advisory）であり、route 判定に使用しない。

## Overblocking risk review（AC7）

hook handler / gate evaluator が blocking decision を出すべき対象と advisory に留めるべき対象を区分する。**正当な AI 作業を過剰に阻止する overblocking は、fail-closed 強化と同じくらい危険である**:

### Hard Invariant — blocking decision を返してよい条件

以下の hard invariant に該当する場合のみ blocking（Stop / SubagentStop / error exit）を返す:

1. **Allowed Paths 逸脱**: Edit / Write / MultiEdit で Allowed Paths 外への変更を検出 → `ALLOWED_PATHS_GATE_RESULT_V1.status: fail_closed` で step を block
2. **Secret / sensitive file**: `.env`、`.git`、credential、private key、API key containing の変更を検出 → `status: blocked` + human_escalation
3. **Destructive command**: `git reset --hard`, `git push --force`, `rm -rf /`, `dd if=/dev/zero` 等の危険操作を検出 → exit nonzero + detailed reason
4. **Routing-critical machine-readable contract 欠落**: `ALLOWED_PATHS_GATE_RESULT_V1.status` が indeterminate で、判定が確定できない → human_escalation

### Advisory に留めるべき条件（blocking してはいけない）

以下の条件では block ではなく advisory / review finding / comment に留める。**正当な AI 作業として認可する**:

- 文体・言語・表現が日本語ルール（`.claude/rules/project-constitution.md`）に不一致
- PR body の format が慣例と異なる（構成、見出し順序、行折り返し、空行）
- コミットメッセージが Conventional Commits でない、またはスコープ・型が慣例と異なる
- レビューコメント / docstring / log メッセージの説明品質が低い
- テスト名が GIVEN/WHEN/THEN 形式でない
- 報告フォーマット（YAML / JSON の キー順序）が異なる
- 参考 URL の引用形式、リスト記号の統一性
- 以上の「スタイル / 報告形式」を理由に実装を止めてはならない（レビュー後に author が修正判断をする権利がある）

### 区分不可能な case 対応

blocking と advisory の境界が曖昧な case が判明した場合:
- 即座に決定を下さず人間に escalate（human_judgment_required）
- 判定基準を `.claude/rules/project-constitution.md` または `.claude/skills/impl-review-loop/references/overblocking-risk-matrix.md` へ追加して deterministic に固定

## Loop-prevention invariant（AC9）

この loop-prevention 規則は、Stop / SubagentStop / TeammateIdle hook が表現・言語・報告品質の不足を理由に継続実行を強制しないことを要求する重要な不変条件である。同時に、block する場合は理由が machine-readable で、次 action が有限・一意かつ human_escalation path を持つ:

Stop / SubagentStop / TeammateIdle hook は表現・言語・報告品質の不足を理由に継続実行を強制しない。同時に、block する場合は理由が machine-readable で、次 action が有限・一意かつ human_escalation path を持つ:

- Stop / SubagentStop：自動 block による無限停止を防ぐ
  - 文体 / 言語エラーを理由に Stop してはいけない（代わり review finding に留める）
  - block する場合：`reason` field は machine-readable enum（例: `Allowed_Paths_violation`）+ human-readable explanation
  - 次 action：明確（例: "worker rerun + edit path.txt"）、有限（"再試行の上限回数"）、escalation path あり（"3 回目失敗時 human_judgment へ"）
  
- SubagentStop：worker から control-plane への transition
  - advisory feedback（文体指摘等）を理由に SubagentStop してはいけない
  - fail-closed decision は structured RESULT スキーマ（`IMPLEMENT_RESULT_V1`, `ALLOWED_PATHS_GATE_RESULT_V1` 等）で emit
  
- TeammateIdle：agent team 文脈での teammate idling 決定
  - 表現品質不足を理由に teammate を idle 化し協力を打ち切ってはいけない
  - block 判定は hard invariant に限定し、fallback skill / escalation path を含める

## Related

- `.claude/skills/implement-issue/SKILL.md` — Step 1 で使う実装手順
- `.claude/skills/pr-review-judge/SKILL.md` — Step 4 で使うレビュー判定手順
- `.claude/skills/open-pr/SKILL.md` — Step 1 内で PR 起票に使う
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 本文改善のループ（本 skill とは別）
- `.claude/agents/implementation-worker.md` / `test-runner.md` / `pr-reviewer.md` — Step 1-4 で委譲する SubAgent
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター設計原則（control-plane / LOOP_STATE / 人間承認原則）
- `docs/dev/github-ops.md` — GitHub 運用ルール（body-file guard / コメントテンプレ）

## Loop Policy 参照

impl-review-loop は `.claude/skills/issue-refinement-loop/references/termination-policy.md` の `LOOP_POLICY_V1` と同一の routing policy を採用する。`max_iterations` 既定値 3、loop iteration approval gate は repo_loop_iteration_only スコープ、Claude Code permission mode は変更しない。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
