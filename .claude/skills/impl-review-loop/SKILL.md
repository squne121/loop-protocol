---
name: impl-review-loop
description: >-
  implementation child issue を **実装→検証→PR レビュー** の 3 ステップループで自律完了させるオーケストレーター。
  Issue 番号を受け取り、pr-reviewer の LOOP_VERDICT が APPROVE になるまで反復する。
  `/impl-review-loop <N>` または「Issue ◯◯ をループで実装して」のトリガーで使う。
  着手前に `docs/dev/workflow.md` の通常 workflow safety boundary を確認する。
---

# Impl Review Loop

## Advisory artifact policy（助言的 artifact の方針、Issue #1830）

scope-rollup、overlap、contract snapshot、body SHA、launch ledger、
session manifest、publish context、controlled-executor receipt は観測情報であり、
存在・freshness・identity・digest の欠落や不正を routing の停止条件にしない。
`SUBAGENT_LAUNCH_LEDGER_V1` は warning のみで、PASS、承認、merge readiness の
証拠として使用禁止とする。routing は live Issue、linked worktree の
cwd/branch/HEAD/dirty state、Allowed Paths、実テスト、CI、PR review に基づける。

implementation child issue を **実装 → 検証 → PR レビュー** の 3 ステップループで自律完了させるオーケストレーター skill。各ステップを SubAgent に委譲し、メインの control-plane（state tracking + routing）に責務を限定する。

## Inputs（入力）

- `issue_number`（必須）: implementation child issue 番号
- `contract_snapshot_url`（任意）: 参照用 telemetry。欠落時に materialize を要求せず routing を継続する。
- `max_iterations`（任意、デフォルト 3）: 上限回数。超過時は fail-close で人間判断を仰ぐ

## Loop Structure（ループ構造）

```
[Step 1: Implementation]  → implementation-worker SubAgent (implement-issue skill)
        ↓
[Step 2: Verification]    → test-runner SubAgent
        ↓
[Step 4: PR Review]       → pr-reviewer SubAgent (pr-review-judge skill)
        ↓
[Step 5: Judgment]        → reviewer_verdict + live_mergeability を route_loop_verdict_v2() で解析
        ↓
    route: approved → 終了（PR は人間がマージ判断）
    route: route_to_update_branch → worker 委譲（update_branch）→ 検証・PR review 再実行
    route: route_stale_head_rereview → 現在 head で PR review 再実行
    route: continue_loop（REQUEST_CHANGES） → Step 1 に戻る（fix_delta を渡す）
    route: route_human_escalation / route_conflict_escalation / fail_closed → 人間判断を仰ぐ
    上限超過 → 人間判断を仰ぐ
```

Issue #1873 以降、pr-reviewer は `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention のみを返す。`merge_ready` / `required_auto_actions` / `mergeability` / `allowed_paths_gate` は reviewer の自己申告として受け取らない。mergeability は control-plane が `gh pr view` で直接取得し、`route_loop_verdict_v2()`（`.claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py`）の `live_mergeability` 引数として渡す。`update_branch` action は reviewer から受け取らず `route_loop_verdict_v2()` が合成する。

> Step 3（adversarial review）と Step 1.5（spec document review）は LOOP_PROTOCOL では採用しない（PR #12 / #20 方針）。Step 番号は履歴互換のため 1 → 2 → 4 → 5 のまま保持する。

## Procedure（手順）

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
  contract_snapshot_source: provided | detected_existing | materialized_by_issue_contract_review
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
  contract_materialization:
    attempted: bool
    source: existing_go | materialized_go | latest_blocked | readiness_blocked | human_judgment | stale_conflict
    result_schema: CONTRACT_SNAPSHOT_ENSURE_RESULT_V1
    contract_snapshot_url: null
    artifact_path: artifacts/contract-snapshot/...
```

## 終了条件

| 条件（`route_loop_verdict_v2()` の `route`） | アクション |
|---|---|
| `approved`（`verdict: APPROVE` かつ live mergeability が `CLEAN`/`HAS_HOOKS` かつ `blockers == []`） | 終了。`IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready` を emit。PR は人間がマージ判断 |
| `route_to_update_branch`（live `merge_state_status == BEHIND`） | 終了しない。合成された `update_branch` action を worker に委譲し、検証・PR review を再実行する |
| `route_stale_head_rereview`（`reviewed_head_sha` が現在の PR head と不一致） | 終了しない。現在 head で PR review を再実行する |
| `continue_loop`（`verdict: REQUEST_CHANGES`） | 終了しない。Step 1 に戻り blockers を fix_delta として渡す |
| `iteration ≥ max_iterations` | fail-close。`termination_reason: max_iterations` を LOOP_STATE に記録、人間判断を仰ぐ |
| `route_human_escalation`（`verdict: HUMAN_REVIEW_REQUIRED`、または `BLOCKED`/`UNSTABLE`/`DRAFT`） | 即停止、人間判断を仰ぐ |
| `route_conflict_escalation`（`mergeable: CONFLICTING` または `merge_state_status` が `DIRTY`/`CONFLICTING`） | CONFLICTING PR Escalation Runbook 参照 |
| `fail_closed`（schema 不正、`APPROVE` かつ `blockers` 非空、mergeability `UNKNOWN` 等） | `reason_code` / `errors` を記録。`UNKNOWN` は bounded retry（最大 3 回）後に人間判断、それ以外は即人間判断 |

> **重要**: `verdict: APPROVE` 単独では終了しない。live mergeability が `CLEAN`/`HAS_HOOKS` かつ `blockers == []` の両条件が必要（`route_loop_verdict_v2()` が判定する）。

## 外部仕様調査の取扱い

外部仕様調査が必要な場合は `gemini-cli-headless-delegation` skill を default 経路として使い、結果を LOOP_STATE の `external_research_skip_basis` に記録する。LOOP_PROTOCOL は internal-only 変更が多い前提のため、デフォルトはスキップで構わない（スキップ時も判定根拠を記録する）。

## Allowed Paths Gate Routing（許可パスゲートのルーティング、Issue #1873）

pr-reviewer が実行する `ALLOWED_PATHS_GATE_RESULT_V1`（決定論的スクリプト、正本は pr-review-judge 配下）の
`status` は、専用 `LOOP_VERDICT_V2.allowed_paths_gate` フィールドとして自己申告されず、
`status != ok` の場合は具体的な違反内容が `reviewer_verdict.blockers[]` にテキストとして含まれる
（`references/allowed-paths-gate.md` 参照）。

| gate `status` | reviewer_verdict への反映 | 結果としての `verdict` | routing |
|---|---|---|---|
| `ok` | blocker を追加しない | `APPROVE` 可 | `route_loop_verdict_v2()` の通常判定へ |
| `fail_closed` | 違反内容を `blockers[]` に記載 | `REQUEST_CHANGES` | `continue_loop`。next iteration で修正 |
| `stale_snapshot` | 「contract snapshot が stale」を `blockers[]` に記載 | `REQUEST_CHANGES` | `continue_loop`。snapshot を refresh して PR レビュー再実行 |
| `indeterminate` | 理由（head SHA mismatch 等）を `blockers[]` に記載 | `REQUEST_CHANGES` または `HUMAN_REVIEW_REQUIRED` | `continue_loop` または `route_human_escalation` |
| malformed（スクリプト実行不能） | 実行不能である旨を `blockers[]` に記載 | `HUMAN_REVIEW_REQUIRED` | `route_human_escalation` |

`verdict: APPROVE` かつ `blockers` が非空は inconsistent な reviewer 結果として `route_loop_verdict_v2()` が
`fail_closed`（`approve_with_blockers_inconsistent`）を返す。つまり `status != ok` のまま `APPROVE` を出す
reviewer 結果は production 経路で自動的に拒否される。

## Contract Snapshot 参照ルール

preparation step で取得した contract snapshot 内の以下の情報を Step 1-4 で参照する:

### VC Preflight Reference（検証コマンド事前確認の参照）

`vc_preflight` JSON（`baseline_vc_preflight.py` が生成）を参照し、impl-review-loop 側で `baseline_vc_preflight.py` を重複実行しない。VC 分類の正本は contract snapshot の `vc_preflight.classifications[]` に従う。

### Product Spec Check Reference（プロダクト仕様確認の参照, Issue #333）

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

## Guardrails（安全策）

- loop policy（何回まで自動で回すか）と Claude Code permission mode（ツール呼び出しの承認方式）は直交する概念であり、loop policy の継続判断に `--permission-mode` / `permissions.defaultMode` / `--dangerously-skip-permissions` を参照しない
- control-plane だけを担い、data-plane 操作（push / `gh pr edit` / マージ等）は SubAgent に委譲する
- LOOP_STATE をイテレーションごとに更新し、人間がループの全履歴を読めるようにする
- `max_iterations` 超過時は必ず fail-close（無限ループ防止）
- adversarial review は採用しないため `LOOP_VERDICT` 判定は pr-review-judge の APPROVE 一本で完結
- 全 SubAgent 出力は構造化フォーマット（YAML / KEY=VALUE）で受け取り、散文サマリで上書きしない
- **contract snapshot advisory routing**（#1851）: `contract_snapshot.normalized_status` が `go` / `missing_go` / `stale` / `runtime_error` のいずれかであれば、本節冒頭の advisory artifact policy に従い `next_action.route` は無条件で `proceed_to_step_1` を返し、live Issue の Allowed Paths と実テスト・CI・PR review に基づいて routing を継続する。`missing_go` / `stale` を検出した場合は参考情報として `ensure_contract_snapshot.py` による再 materialize を試みてよいが、その成否や `status: human_judgment` / `blocked_needs_refinement` / `stale_or_conflicting_snapshot` は routing の停止条件にしない。`latest_blocked`（trusted author による明示 blocked/request_changes）のみ人間判断（`run_contract_blocker_triage`）へ route する human veto 境界として維持する。

## Related（関連ファイル）

- `.claude/skills/implement-issue/SKILL.md` — Step 1 で使う実装手順
- `.claude/skills/pr-review-judge/SKILL.md` — Step 4 で使うレビュー判定手順
- `.claude/skills/open-pr/SKILL.md` — Step 1 内で PR 起票に使う
- `.claude/skills/issue-refinement-loop/SKILL.md` — Issue 本文改善のループ（本 skill とは別）
- `.claude/agents/implementation-worker.md` / `test-runner.md` / `pr-reviewer.md` — Step 1-4 で委譲する SubAgent
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター設計原則（control-plane / LOOP_STATE / 人間承認原則）
- `docs/dev/github-ops.md` — GitHub 運用ルール（body-file guard / コメントテンプレ）
- `docs/dev/agent-run-report.md` — run report finalize / posting handoff 規約
- `docs/dev/agent-retro-index.md` — retro index 更新規約

## Loop Policy 参照

impl-review-loop は `.claude/skills/issue-refinement-loop/references/termination-policy.md` の `LOOP_POLICY_V1` と同一の routing policy を採用する。`max_iterations` 既定値 3、loop iteration approval gate は repo_loop_iteration_only スコープ、Claude Code permission mode は変更しない。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
