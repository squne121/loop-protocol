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

## Root-Owned Synchronous Entry Transition（Step 1 起動契約, #2272 正本、root-direct 再設計）

Step 1（Implementation）の起動は、root/main thread が単一の継続した invocation
（in-process 呼び出しなら同一 call stack、CLI subprocess 経由なら同一 continuous
turn。詳細は `issue-refinement-loop/references/termination-policy.md` の
production carrier `delivery` 定義参照）の中で、
capability preflight → live Issue fetch → 同一呼び出し内での current-run
`issue-contract-review`（`.claude/skills/issue-refinement-loop/scripts/root_entry_router.py`
の `run_root_transition()` が既存 `run_once()` を関数として直接呼び出す。
`.claude/skills/issue-contract-review/**` 自体は変更しない）→ 直後の live 再取得
→ routing 決定 → 条件成立時のみ同じ呼び出しの中で Step 1 を直接起動する、という
一続きの手順を自ら実行した場合にのみ許可される。producer/consumer を跨いで
再提示可能な `invocation_token` は撤去済みで、`ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1`
は authorization packet ではなく非永続の process-local route result であり、
GitHub comment・artifact・digest・invocation ID のいずれも単独では Step 1 起動を
authorize しない（`issue-refinement-loop/references/termination-policy.md` の
「Root-Owned Synchronous Entry Transition」節が normative SSOT）。旧
`implementation_entry_decision`（authorization packet 型）および producer/consumer
subprocess 分離方式（`invocation_token` による再提示）は撤回済み。

## Inputs（入力）

- `issue_number`（必須）: implementation child issue 番号
- `contract_snapshot_url`（任意）: 参照用 telemetry。欠落時に materialize を要求せず routing を継続する。
- `max_iterations`（任意、デフォルト 3）: 上限回数。超過時は fail-close で人間判断を仰ぐ
- `human_context_comment_urls`（任意、repeatable, #1950 AC6）: root/main thread が「人間が投稿した自然言語コンテキスト」として明示的に渡す Issue comment URL のリスト。origin はコメント本文・投稿アカウント・`author_association`・構造化 marker の有無から推測せず、**この引数として渡されたこと自体だけ**を origin 判定根拠にする。
- `agent_report_comment_urls`（任意、repeatable, #1950 AC6）: root/main thread が「SubAgent が返した構造化 report」として明示的に渡す Issue comment URL のリスト。同様に本引数として渡されたことだけが origin 判定根拠であり、投稿アカウントが human_context 側と同一でも構わない（例: `create-issue transaction partial-failure` のような機械生成コメントと人間コメントが同一アカウントから混在する場合がある）。
- 同一 URL が `human_context_comment_urls` と `agent_report_comment_urls` の両方に渡された場合は provenance conflict として fail-closed にする（`build_intake_capsule.py` の `--human-context-comment-url` / `--agent-report-comment-url` 参照）。

## Loop Structure（ループ構造）

```
[Step 1: Implementation]  → implementation-worker SubAgent (implement-issue skill)
        ↓
[Step 2: Verification]    → test-runner SubAgent
        ↓ (current-head gate: VC_ADJUDICATION_RESULT_V1.blocking == false のときのみ通過。Issue #88)
[Step 4: PR Review]       → pr-reviewer SubAgent (pr-review-judge skill)
        ↓
[Step 5: Judgment]        → reviewer_verdict + live_mergeability を route_loop_verdict_v2() で解析
        ↓
    route: approved → 終了（PR は人間がマージ判断）
    route: route_to_update_branch → worker 委譲（update_branch）→ 検証・PR review 再実行
    route: route_stale_head_rereview → 現在 head で PR review 再実行
    route: continue_loop（REQUEST_CHANGES） → Step 1 に戻る（fix_delta を渡す）
    route: route_human_escalation（verdict: HUMAN_REVIEW_REQUIRED） → 人間判断を仰ぐ
    route: conflict_hard_stop（actual conflict のみ） → CONFLICTING PR Escalation Runbook
    route: fail_closed（schema 不正 / UNKNOWN / BLOCKED / UNSTABLE / DRAFT 等） → warning 記録、次サイクルで再評価（自動 human escalation ではない）
    上限超過 → 人間判断を仰ぐ
```

Issue #1873 以降、pr-reviewer は `verdict` / `reviewed_head_sha` / `blockers` / `warnings` の最小 convention のみを返す。`merge_ready` / `required_auto_actions` / `mergeability` / `allowed_paths_gate` / `test_verdict` は reviewer の自己申告として受け取らない。mergeability は control-plane が `gh pr view` で直接取得し、`route_loop_verdict_v2()`（`.claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py`）の `live_mergeability` 引数として渡す。`update_branch` action は reviewer から受け取らず `route_loop_verdict_v2()` が合成する。Step 1-4 の SubAgent が返す `human_review_required: true`（真偽値の自己申告）自体には停止権限がない（#1860 Owner Decision）。詳細は `step-5-feedback-and-termination.md` の「human_review_required の扱い」を参照。

> Step 3（adversarial review）と Step 1.5（spec document review）は LOOP_PROTOCOL では採用しない（PR #12 / #20 方針）。Step 番号は履歴互換のため 1 → 2 → 4 → 5 のまま保持する。

## Procedure（手順）

各 Step の詳細は `steps/` 配下に分割。実行時は下記の順で読む:

1. [事前準備（state 初期化・worktree 確認）](steps/preparation.md)
2. [Step 1: Implementation](steps/step-1-implementation.md)
3. [Step 2: Verification](steps/step-2-verification.md)
4. [Step 4: PR Review](steps/step-4-pr-review.md)
5. [Step 5: 判定・終了・フィードバック循環](steps/step-5-feedback-and-termination.md)
6. [Step 5: LOOP_VERDICT ルーティング（mergeability handling）](steps/step-5-mergeability-handling.md)
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
  last_loop_verdict: APPROVE | REQUEST_CHANGES | HUMAN_REVIEW_REQUIRED | null
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
| `continue_loop`（`verdict: REQUEST_CHANGES`、actual conflict がない場合） | 終了しない。Step 1 に戻り blockers を fix_delta として渡す |
| `iteration ≥ max_iterations` | fail-close。`termination_reason: max_iterations` を LOOP_STATE に記録、人間判断を仰ぐ |
| `route_human_escalation`（`verdict: HUMAN_REVIEW_REQUIRED`、actual conflict がない場合） | 即停止、人間判断を仰ぐ |
| Step 1-4 のいずれかで `human_review_required: true`（真偽値の自己申告）を SubAgent が返した | #1860 Owner Decision により即停止しない。warning として記録し、iteration 余裕があれば継続する（`step-5-feedback-and-termination.md` の「human_review_required の扱い」参照）。ループを止める human veto は live Issue/PR コメント上の明示的な停止指示、または実 Git conflict／target PR mergeability に限定する |
| `conflict_hard_stop`（`mergeable == CONFLICTING` または `merge_state_status == DIRTY`。**verdict に関係なく最優先で評価**） | CONFLICTING PR Escalation Runbook 参照（`merge_state_status == CONFLICTING` は無効な enum 値であり schema 不正として扱う。`BLOCKED` は required checks/review 未充足であり Git conflict ではないため本 runbook の対象にしない） |
| `fail_closed`（schema 不正、`APPROVE` かつ `blockers` 非空、mergeability `UNKNOWN`、`BLOCKED`/`UNSTABLE`/`DRAFT`） | `reason_code` を warning として記録。`UNKNOWN` は bounded retry（最大 3 回）後も warning のまま継続。`BLOCKED`/`UNSTABLE`/`DRAFT` は current-head required-CI / branch-protection evaluator の判定に委ね、human escalation にはしない |

> **重要**: `verdict: APPROVE` 単独では終了しない。live mergeability が `CLEAN`/`HAS_HOOKS` かつ `blockers == []` の両条件が必要（`route_loop_verdict_v2()` が判定する）。

## 外部仕様調査の取扱い

外部仕様調査が必要な場合は `gemini-cli-headless-delegation` skill を default 経路として使い、結果を LOOP_STATE の `external_research_skip_basis` に記録する。LOOP_PROTOCOL は internal-only 変更が多い前提のため、デフォルトはスキップで構わない（スキップ時も判定根拠を記録する）。

## Allowed Paths Gate Routing（許可パスゲートのルーティング、Issue #1873）

pr-reviewer が実行する `ALLOWED_PATHS_GATE_RESULT_V1`（決定論的スクリプト、正本は pr-review-judge 配下）の
`status` は専用フィールドとして `reviewer_verdict` に自己申告されず（`LOOP_VERDICT_V2` は #1873/#1875 で完全撤去済みであり本節が参照する対象は存在しない）、
`status != ok` の場合は具体的な違反内容が `reviewer_verdict.blockers[]` にテキストとして含まれる
（`references/allowed-paths-gate.md` 参照）。gate の pattern source は常に **live linked Issue 本文**
であり、contract snapshot / `expected_contract_fingerprint` は advisory telemetry に過ぎない
（欠落・不一致のみを理由に block しない。`allowed-paths-gate.md` 参照）。gate 自体（path が
Allowed Paths 外か、rename/copy provenance が確定できるか）は hard safety boundary のまま維持する。

gate `status` の値は `ok` / `fail_closed` / `indeterminate` のみ（`stale_snapshot` は `status` を
占有しない -- contract fingerprint drift は `warnings[]` の advisory annotation としてのみ表れ、
live 本文で評価した `status` が canonical のまま変わらない。詳細は `references/allowed-paths-gate.md`
参照）。

| gate `status` | reviewer_verdict への反映 | 結果としての `verdict` | routing |
|---|---|---|---|
| `ok` | blocker を追加しない（fingerprint drift があれば `warnings[]` に advisory 記載） | `APPROVE` 可 | `route_loop_verdict_v2()` の通常判定へ |
| `fail_closed` | 違反内容を `blockers[]` に記載 | `REQUEST_CHANGES` | `continue_loop`。next iteration で修正 |
| `indeterminate` | 理由（path が Allowed Paths 外と確定できない等）を `blockers[]` に記載 | `REQUEST_CHANGES` または `HUMAN_REVIEW_REQUIRED` | `continue_loop` または `route_human_escalation` |
| malformed（スクリプト実行不能） | 実行不能である旨を `blockers[]` に記載 | `HUMAN_REVIEW_REQUIRED` | `route_human_escalation` |

`verdict: APPROVE` かつ `blockers` が非空は inconsistent な reviewer 結果として `route_loop_verdict_v2()` が
`fail_closed`（`approve_with_blockers_inconsistent`）を返す。つまり Allowed Paths 違反が確定した状態のまま
`APPROVE` を出す reviewer 結果は production 経路で自動的に拒否される。

## Contract Snapshot 参照ルール

preparation step で取得した contract snapshot 内の以下の情報を Step 1-4 で参照する:

### VC Preflight Reference（検証コマンド事前確認の参照）

`vc_preflight` JSON（`baseline_vc_preflight.py` が生成）を参照し、impl-review-loop 側で `baseline_vc_preflight.py` を重複実行しない。VC 分類の正本は contract snapshot の `vc_preflight.classifications[]` に従う。

### Product Spec Check Reference（プロダクト仕様確認の参照, Issue #333）

`checks.product_spec_check` を contract snapshot から読み取り、Step 1 delegation 前に `LOOP_STATE.product_spec_preflight` に正規化して格納する。以下のルールに従う（#1869 fix_delta P0-4: `stop_human` / `refresh_contract_snapshot` は advisory warning へ改訂。product-spec snapshot は semantic planning artifact であり、それ自体には停止権限がない）:

> **注意**: `refresh_contract_snapshot` は **route only; no auto-run** の warning である（AI が `issue-contract-review` を自動実行することはない）。人間へ「再実行を推奨する」旨を記録するに留め、Step 1 continuation は妨げない。

- `checks.product_spec_check` が snapshot に存在しない場合は stale / incomplete snapshot として warning を記録し（`routing_action: refresh_contract_snapshot`）、Step 1 へ継続する
- `applicability == not_applicable && decision == pass` の場合、無関係 Issue として `continue` へ継続
- `applicability == not_applicable && decision != pass` は inconsistent snapshot として warning を記録し（`routing_action: refresh_contract_snapshot`）、Step 1 へ継続する
- `decision == fail` → warning として記録し（`routing_action: stop_human` は advisory 表示のみ）、Step 1 へ継続する。live Issue/PR コメント上の明示的な人間の停止指示がある場合のみ実際に停止する
- `decision == human_judgment` → 同上（warning として記録し継続。明示的な人間の停止指示がある場合のみ停止）
- `decision == pass` かつ `applicability == applicable` → 続行、`routing_action: continue`
- 不正な enum 値 → stale / invalid snapshot として warning を記録し（`routing_action: refresh_contract_snapshot`）、Step 1 へ継続する

**実装例**: `.claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py` が mutation-free CLI として `PRODUCT_SPEC_GATE_DECISION_V1` を出力する（routing_action: continue | stop_human | refresh_contract_snapshot。いずれも advisory であり `continue` 以外も Step 1 continuation を妨げない）。

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
- `docs/dev/workflows/impl-review-loop-design.md` — 設計判断・failure mode の詳細（`derived_design_note`。本 entrypoint と矛盾する場合は本 entrypoint が正本、#1876）

## Loop Policy 参照

impl-review-loop は `.claude/skills/issue-refinement-loop/references/termination-policy.md` の `LOOP_POLICY_V1` と同一の routing policy を採用する。`max_iterations` 既定値 3、loop iteration approval gate は repo_loop_iteration_only スコープ、Claude Code permission mode は変更しない。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
