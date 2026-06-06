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
-->

`issue-refinement-loop` は control-plane 専用の thin entrypoint である。詳細 procedure は `references/` を必要時だけ読む progressive disclosure とし、planner / reviewer / worker の判定ロジックをこのファイルへ再実装しない。

## Inputs

- `issue_number`（必須）: 改善対象の Issue 番号
- `max_iterations`（任意、既定 3）: review cycle の上限
- `anchor_comment_url`（任意）: 人間 Decision や差し戻しコメントを snapshot 固定して扱う対象コメント URL

## Loop Policy

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

## Loop Structure

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
 needs-fix
   ├─ iteration + 1 < max_iterations:
   │    iteration += 1 → Step 4 (自動継続)
   │
   └─ iteration + 1 >= max_iterations:
        → Step 5 (human_escalation) + 全 iteration 分 blocker summary
```

Step 3（adversarial review）と Step 1.5（spec document review）は採用しない。Step 番号は履歴互換のため維持する。

## LOOP_STATE Summary

```yaml
LOOP_STATE:
  issue_number: <int>
  iteration: <int, 0-indexed>
  max_iterations: 3
  last_verdict: approve | needs-fix | null
  blockers_history: []
  improvements_applied: []
  removed_state_labels: []
  termination_reason: null | approved | human_escalation | superseded_by_decision
  scope_rollup_decision: null
  anchor_comment:
    url: null
    preliminary_classification: null
    final_classification: null
    classification_reason: null
    verified_claims: []
    unresolved_claims: []
    scope_impact: null
    requires_fact_check: false
  investigation_policy:
    required: false
    codebase_reason: null
    target_paths: []
    repo_claims: []
    skip_reason: null
  scope_signal_guard:
    triggered: false
    excluded_by_anchor_reframe: false
    reason_code: null
  web_research_policy:
    required: false
    reason: null
    critical_external_claims: []
    skip_reason: null
  web_research:
    required: false
    status: null
    failure_class: null
    verification_route: null
    result: null
  product_spec_context:
    detected: false
    work_kind: null
    signals: []
  delivery_rollup:
    applicable: false
    unmaterialized_slots: []
  follow_up_materialization:
    candidates: []
  superseded_decision:
    decision_summary: null
    alternative_issue_number: null
    close_reason: null
```

詳細なフィールド定義と routing semantics は `references/` 側の owner file を参照する。

## Procedure

### Step 0: Preconditions

1. Issue 本文と必要コメントを取得し、`state/needs-human` / `state/done` の hard stop を確認する。
2. `anchor_comment_url` がある場合は snapshot を固定し、対象 Issue 所属を検証する。
3. scope rollup preflight を mutation-free で実行し、`LOOP_STATE.scope_rollup_decision` を記録する。
4. Product/Spec routing signal を検知し、`LOOP_STATE.product_spec_context` を更新する。
5. 本 Issue への refinement 継続が確定した後に、stale な `state/blocked` / `state/queued` を hygiene として除去する。

### Step 0f: Planner Consumption

`plan_refinement_loop.py` を実行して `REFINEMENT_LOOP_PLAN_V1` を生成し、JSON schema validation を通す。`fail_closed.required == true` の場合は停止し、人間判断へ送る。`investigation_policy` / `web_research_policy` / `scope_signal_guard` / `follow_up_materialization` の判定は planner を SSOT とし、このファイルで prose 再判定しない。

参照:

- `references/refinement-loop-plan-output.md`
- `references/scope-signal-guard.md`

### Step 1: Investigation

`REFINEMENT_LOOP_PLAN_V1.decisions.investigation_policy.required == true` の場合のみ `codebase-investigator` を起動する。返却される構造化結果を受け取り、`final_classification` の確定責務は main thread が保持する。SubAgent は mutation してはいけない。

anchor comment の fact-check 契約、`ANCHOR_COMMENT_CONTEXT_V1`、`ANCHOR_COMMENT_FACT_CHECK_RESULT_V1`、`REPO_EVIDENCE_REF_V1` の扱いは `references/anchor-comment-handling.md` を参照する。

### Step 1b: Web Research

`REFINEMENT_LOOP_PLAN_V1.decisions.web_research_policy.required == true` の場合のみ `web-researcher` を起動する。orchestrator は `WEB_RESEARCH_RESULT_V1` を opaque に扱い、consumer field だけを `LOOP_STATE.web_research` へ反映する。retry / fallback / raw grounding state は保持しない。

詳細は `references/web-research-routing.md` を参照する。

### Step 2: Review

`issue-reviewer` SubAgent が `review-issue` を実行し、`REVIEW_ISSUE_RESULT_V1` を返す。orchestrator は `status` と `verdict` だけで routing し、domain judgment を再解釈しない。

anchor comment により stale approval を無効化する場合も、raw snapshot は Step 4 に渡さず、正規化済み `anchor_comment_feedback` だけを渡す。

### Step 4: Rewrite

`issue-author` SubAgent に opaque forwarding payload を渡して本文を更新する。AC/VC の baseline fail expectation と review 時の扱いを取り違えないこと。詳細な reflection guard は `references/ac-vc-reflection.md` を参照する。

### Step 4.5: Child / Follow-up Materialization

delivery-rollup parent の child materialization gate と、approve 後の follow-up 起票候補は `references/follow-up-materialization.md` を参照する。dedupe は title ではなく `dedupe_key` で行う。

### Step 5: Termination

終了条件、`human_escalation` 経路、scope change signal 停止、loop termination table は `references/termination-policy.md` を参照する。

`approved` 終了時は `LOOP_HANDOFF_RESULT_V1` marker を終了コメントに出力する（形式・routing rules は `references/termination-policy.md#LOOP_HANDOFF_RESULT_V1` 参照）。出力は `<!-- LOOP_HANDOFF_RESULT_V1 -->` HTML comment と fenced YAML block の 2 要素。

#### Termination Report Publish Flow

終了レポートの GitHub 投稿は `publish_termination_report.py` を経由して行う。

```bash
# TERMINATION_REPORT_INPUT_V1 JSON を stdin から渡す
echo '{"termination_reason":"approved","issue_number":42}' | \
  python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py \
    --issue-number 42
```

`publish_termination_report.py` は以下の責務を持つ:

1. `render_termination_report.py` を `subprocess.run([sys.executable, ...], shell=False, ...)` で呼び出す
2. stdout JSON の `schema` / `schema_version` / `publishable` / `body` / `reason_code` を検証する
3. `publishable=true` かつ `body` が非空文字列の場合のみ `gh issue comment --body-file` を呼ぶ
4. `publishable=false`、renderer 異常、validation 失敗の場合は gh を呼ばず fail-closed で終了し、reason_code / timestamp をローカル artifact に記録する

詳細な publisher 仕様は `.claude/skills/issue-refinement-loop/scripts/publish_termination_report.py` を参照する。

## Reference Map

| topic | primary reference |
|---|---|
| anchor comment handling | `references/anchor-comment-handling.md` |
| scope signal guard | `references/scope-signal-guard.md` |
| AC/VC reflection | `references/ac-vc-reflection.md` |
| follow-up materialization | `references/follow-up-materialization.md` |
| web research routing | `references/web-research-routing.md` |
| termination policy | `references/termination-policy.md` |
| planner output contract | `references/refinement-loop-plan-output.md` |
| scope rollup preflight | `references/scope-rollup-policy.md` |

## Guardrails

- thin entrypoint を維持し、判定ロジックは planner / reviewer / worker の SSOT を consume するだけに留める
- control-plane のみを担当し、Issue/PR mutation や final judgment の一部を SubAgent に委譲しすぎない
- raw anchor comment snapshot を reviewer feedback や title rewrite 入力へ直接流さない
- `WEB_RESEARCH_RESULT_V1` の retry/fallback/attempt log は link-only とし、`#394` の責務へ越境しない
- `max_iterations` 超過時は fail-close する

## Scope Change Stop Conditions

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
uv run pytest .claude/skills/issue-refinement-loop/tests/test_thin_entrypoint.py -v

# AC7
pnpm typecheck
pnpm lint
pnpm test
pnpm build
uv run pytest .claude/skills/issue-refinement-loop/tests/ -v
```

## Related

- `.claude/skills/review-issue/SKILL.md`
- `.claude/skills/edit-issue/SKILL.md`
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md`
- `docs/dev/agent-skill-boundaries.md`
- `docs/dev/workflow.md`

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドを削らず、人間向け説明・証跡の再掲のみを削減する。
