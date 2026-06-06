# Termination Policy

## Loop end conditions

| condition | termination_reason |
|---|---|
| Step 2 returns `approve` AND latest `CONTRACT_REVIEW_RESULT_V1.status == "go"` confirmed | `approved` |
| Step 2 returns `approve` BUT latest `CONTRACT_REVIEW_RESULT_V1.status != "go"` | continue (re-run `issue-contract-review`) |
| Step 2 returns `needs-fix` and `iteration + 1 < max_iterations` | continue to next iteration |
| Step 2 returns `needs-fix` and `iteration + 1 >= max_iterations` | `human_escalation` (with full blocker summary) |
| Any step requires human review | `human_escalation` |
| `final_classification == superseded_by_decision` and close / replacement flow completed | `superseded_by_decision` |

## Contract Hygiene Repair Routing Predicate

`ISSUE_AUTHOR_RESULT_V1.contract_hygiene_repair_applied` フラグによる iteration accounting ルール。

| `contract_hygiene_repair_applied` | `no_change` guard | routing |
|---|---|---|
| `true` | — | semantic iteration を消費しない。Step 2（reviewer）に戻す（iteration カウントを increment しない） |
| `false` | — | 通常通り iteration カウント（semantic iteration として処理） |
| `true` だが body_sha256 が前回と同一（`no_change`） | 同一 | 同一 lane に戻さない。通常 iteration カウントとして処理（無限ループ防止） |

**重要**: orchestrator は C4/C9 の具体修復知識を持たない。`contract_hygiene_repair_applied: true` フラグのみで routing を判断する。修復の詳細は `edit-issue` skill および `issue-author` SubAgent の責務。

## Handoff State: `refinement_approved_gate_pending` / `implementation_ready`

`issue-reviewer` が `approve` を返しただけでは `implementation_ready` とならない。  
以下の 2 段階を経て初めて handoff 状態が `implementation_ready` に遷移する:

| handoff 状態 | 条件 |
|---|---|
| `refinement_approved_gate_pending` | `issue-reviewer approve` を受け取ったが、`CONTRACT_REVIEW_RESULT_V1.status == "go"` がまだ確認されていない |
| `implementation_ready` | `approve` かつ `CONTRACT_REVIEW_RESULT_V1.status == "go"` かつ freshness 確認済み（`body_sha256` 一致、またはフォールバックとして `generated_at >= issue.updated_at`） |

**重要制約**: `issue-reviewer approve` のみを根拠に `implementation_ready` を返してはならない。  
`issue-contract-review` の `status: go` 確認なしに `impl-review-loop` へ handoff することは禁止（#561 型 handoff gap の防止）。

```yaml
HANDOFF_STATE_V1:
  refinement_approved_gate_pending:
    description: "issue-reviewer approve 済みだが CONTRACT_REVIEW_RESULT_V1.status == go 未確認"
    allowed_next: [run_issue_contract_review, human_escalation]
    forbidden_next: [impl_review_loop_handoff]
  implementation_ready:
    description: "approve かつ status: go かつ freshness 確認済み"
    allowed_next: [impl_review_loop_handoff]
```

## Final Gate — `CONTRACT_REVIEW_RESULT_V1.status == "go"` 必須

reviewer が `approve` を返しても、最新の `CONTRACT_REVIEW_RESULT_V1.status == "go"` が確認できるまで `approved` 終了としない。

- `approve` 後、`issue-contract-review` を実行し `CONTRACT_REVIEW_RESULT_V1.status == "go"` を確認してから完了とする
- `status: blocked` の場合は `approved` ではなく継続（blocker 解消後に `issue-contract-review` 再実行）とする
- `next_action: human_judgment` の場合は `human_escalation` とする（`CONTRACT_REVIEW_RESULT_V1.status` は `go | blocked` のみ。`human_judgment` は `next_action` フィールドで表現）
- 本ルールは `issue-refinement-loop/SKILL.md` が本ファイルを normative reference として消費するため、SKILL.md を変更せずとも実効性がある

### implement-issue Handoff Gate

| `CONTRACT_REVIEW_RESULT_V1` フィールド | handoff 判定 |
|---|---|
| `status: go` | `impl-review-loop` へ handoff 可 |
| `status: blocked` AND `next_action: propose_refinement_loop` | 継続（blocker 解消後に `issue-contract-review` 再実行） |
| `status: blocked` AND `next_action: human_judgment` | `human_escalation` で停止 |

`CONTRACT_REVIEW_RESULT_V1.status` の有効値は `go | blocked`。`human_judgment` は `next_action` フィールドに現れる（`status` フィールドには存在しない）。

### Contract Snapshot Idempotency

- contract-review snapshot comment は Issue body の `body_sha256` を含む
- `body_sha256` が現在の Issue body と一致しない場合（stale result）、その snapshot は無効とする
- stale snapshot を `go` 判定として使用してはならない（`issue-contract-review` を再実行すること）
- Issue body が 1 文字でも変更された場合は `body_sha256` が変化するため、prior snapshot は自動的に stale となる

**Note（policy-only — follow-up 依存）**: `body_sha256` フィールドの producer-side 実装（`issue-contract-review/SKILL.md` の `CONTRACT_REVIEW_RESULT_V1` 出力への追加）は本 Issue のスコープ外。現時点では本セクションは policy constraint として機能し、実装は follow-up Issue で対応する（`issue-contract-review` の out-of-scope 修正として別 Issue を起票すること）。
それまでの間、consumer 側は `CONTRACT_REVIEW_RESULT_V1.generated_at` と Issue の `updated_at` の比較を用いた暫定的な stale 検知を行う。

## Human Escalation on max_iterations

`iteration + 1 >= max_iterations` かつ approve なしの場合は `human_escalation` で停止し、全 iteration 分の blocker summary を終了コメントに添付する。`max_iterations=3` 既定では、3 回目の `needs-fix` で停止する。

## Additional stop rules

- anchor comment fact-check が未完了のまま stale approval を使おうとした場合
- scope change signal が新規追加された場合
- required external research が critical claim を unresolved のまま残した場合

## Must not

- `approve` 以外を success 扱いして silently finish しない
- `max_iterations` を超えて自動ループしない
- hard stop 条件（`state/needs-human`、scope change 等）をスキップしない

## Termination Result Schema（LOOP_TERMINATION_RESULT_V1）

`human_escalation` 終了時は以下の構造で終了コメントを出力する:

```yaml
LOOP_TERMINATION_RESULT_V1:
  termination_reason: human_escalation
  max_iterations: 3
  blockers_history:
    - iteration: 0
      blockers: []
    - iteration: 1
      blockers: []
    - iteration: 2
      blockers: []
```

## Termination Comment（全 termination reason 共通）

すべての termination reason（`approved` / `human_escalation` / `superseded_by_decision`）で、終了コメントに `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を含める。follow-up が存在しない場合も空配列で出力する（`follow_up_issues: []` / `note_only_observations: []`）。

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: issue-refinement-loop
  follow_up_issues: []   # 起票済み / reuse / skip 結果。空の場合も省略しない
  note_only_observations: []  # 起票せず記録のみ。空の場合も省略しない
```

詳細 schema は `docs/dev/agent-skill-boundaries.md` の `FOLLOW_UP_MATERIALIZATION_RESULT_V1` を参照。`issue-refinement-loop` は thin orchestrator として raw context を保持せず、materialization 結果のみを報告する（`docs/dev/agent-skill-boundaries.md` の `ORCHESTRATOR_IO_BOUNDARY_V1` 参照）。

## Loop Policy（LOOP_POLICY_V1）

```yaml
LOOP_POLICY_V1:
  max_iterations_default: 3
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
  routes:
    - when: hard_stop_triggered
      action: human_escalation
    - when: "verdict == 'approve' and contract_review.status == 'go' and contract_review.body_sha256 == issue.body_sha256"
      action: done
    - when: "verdict == 'approve' and contract_review.status != 'go'"
      action: rerun_issue_contract_review
    - when: "contract_review.body_sha256 != issue.body_sha256"
      action: rerun_issue_contract_review
    - when: "verdict == 'needs-fix' and iteration_plus_one < max_iterations"
      action: continue
    - when: "verdict == 'needs-fix' and iteration_plus_one >= max_iterations"
      action: human_escalation
  hard_stops:
    - state/needs-human
    - state/done
    - scope_change_signal
    - contract_malformation
    - required_external_research_unresolved
    - unsafe_mutation
```

## LOOP_HANDOFF_RESULT_V1 — Terminal Contract (SSOT)

`issue-refinement-loop` が `approved` 終了するとき、終了コメントに `LOOP_HANDOFF_RESULT_V1` marker を出力する。  
本セクションが `LOOP_HANDOFF_RESULT_V1` の唯一の SSOT である。

### Output Format

出力は HTML comment と fenced YAML block の 2 要素で構成する:

````
<!-- LOOP_HANDOFF_RESULT_V1 -->
```yaml
LOOP_HANDOFF_RESULT_V1:
  status: impl_ready | human_judgment_required | blocked
  ...
```
````

`<!-- LOOP_HANDOFF_RESULT_V1 -->` HTML comment が marker の開始行を示す。  
fenced YAML ブロックが marker の内容を保持する。

### Schema（JSON Schema: `schemas/loop_handoff_result_v1.json`）

```yaml
LOOP_HANDOFF_RESULT_V1:
  status: impl_ready | human_judgment_required | blocked
  routing_action: run_impl_review_loop | ask_human | blocked
  contract_review:
    status: go | blocked          # CONTRACT_REVIEW_RESULT_V1.status を echo（衝突回避のため separated）
    gate_result: fresh_go | missing_go | stale_go | invalidated_by_request_changes | blocked
    latest_comment_url: string
    generated_at: ISO-8601
    body_sha256: string
  metadata:
    title_prefix_ready: bool
    phase_label_ready: bool
  auto_fixes:
    result: auto_fixed | human_judgment_required | blocked
    required:
      - kind: template_hygiene | metadata_hygiene | known_marker_fix | stale_state_label_cleanup | contract_snapshot_materialization
        executor: implementation-worker
        result: applied | skipped | failed
        evidence:
          before: string
          after: string
          comment_url: string
    skipped: []
  blockers:
    - kind: string
      description: string
  permissions:
    unavailable: []
  generated_at: ISO-8601
  # --- AC11 フィールド（SSOT: 本セクション） ---
  # 以下 4 フィールドは issue-contract-review の終了チェックスクリプトが生成・消費する。
  # runtime enforcement（max_rewrite_attempts / no-progress detection）は #664 の責務であり、
  # 本ファイルはスキーマ定義（SSOT）のみを担う。
  checked_body_sha256: string    # チェック対象の Issue body の SHA-256 ハッシュ
  checker_exit_code: int         # チェックスクリプトの終了コード（0: pass, 1: fail）
  missing_sections: []           # 不足しているセクション名のリスト（pass 時は空）
  missing_contract_keys: []      # 不足している contract キーのリスト（pass 時は空）
```

**Note**: `checked_body_sha256` / `checker_exit_code` / `missing_sections` / `missing_contract_keys` の 4 フィールドに対する runtime enforcement（`max_rewrite_attempts` 制限・no-progress detection 等）は **#664 の責務** であり、本 Issue のスコープ外。本セクションはスキーマの SSOT として機能するのみ。

### `impl_ready` 定義

`status: impl_ready` を出力できるのは以下のすべてが真のときのみ:

1. `contract_review.gate_result == fresh_go` — 最新の `CONTRACT_REVIEW_RESULT_V1.status == "go"` が存在し、現 Issue body hash に対して fresh
2. `contract_review.status == go` が後続の `request_changes` / `blocked` により無効化されていない
3. `metadata.title_prefix_ready == true` または `auto_fixes.required` に `metadata_hygiene` / `template_hygiene` の `result: applied` エントリが存在する
4. `metadata.phase_label_ready == true` または同上の auto-fix applied エントリが存在する
5. `auto_fixes.required` が空（または全 applied 済み）かつ `auto_fixes.skipped` が空
6. `blockers` が空
7. `routing_action == run_impl_review_loop`

**Title prefix / phase label 不在のみを理由に `impl_ready` を拒否してはならない** — implementation-worker (repair mode) が auto-fix evidence を添付していれば `impl_ready` は許可される。

`auto_fixes.required` / `auto_fixes.skipped` の各エントリは `kind` / `executor` / `result` / `evidence`（`before` / `after` / `comment_url`）を含む。`result: skipped` または `evidence` 欠如 → `impl_ready` 禁止。

### Routing Rules

| 条件 | `status` | `routing_action` |
|---|---|---|
| 全 invariant 満足（上記 1〜7） | `impl_ready` | `run_impl_review_loop` |
| `contract_review.gate_result` が `missing_go` / `stale_go` | `blocked` | `blocked` |
| `request_changes` / `blocked` が `go` を後続で無効化 | `blocked` | `blocked` |
| scope / goal / AC に semantic change が検出された | `human_judgment_required` | `ask_human` |
| `blockers` に 1 件以上 | `blocked` | `blocked` |
| fixer unavailable かつ title/label 不在 | `human_judgment_required` | `ask_human` |
| `auto_fixes.skipped` に 1 件以上 | `human_judgment_required` | `ask_human` |

### `human_judgment_required` 停止条件

scope / goal / AC への semantic change が検出されたとき、`issue-refinement-loop` は `LOOP_HANDOFF_RESULT_V1.status: human_judgment_required` / `routing_action: ask_human` で停止し、人間の判断を仰ぐ。Semantic change の検出は `references/scope-signal-guard.md` の guard 定義を参照する。

### Hygiene Delegation Contract（routing 定義のみ）

以下の 5 種 hygiene は implementation-worker (repair mode) に委譲する:

| kind | 委譲先 | 委譲条件 |
|---|---|---|
| `template_hygiene` | implementation-worker (repair mode) | 既定テンプレートセクション欠落 |
| `metadata_hygiene` | implementation-worker (repair mode) | title prefix / phase label 不在 |
| `known_marker_fix` | implementation-worker (repair mode) | 既知の壊れた marker 形式を検出 |
| `stale_state_label_cleanup` | implementation-worker (repair mode) | stale `state/blocked` / `state/queued` を検出 |
| `contract_snapshot_materialization` | implementation-worker (repair mode) | contract snapshot comment 未作成 |

各委譲は `auto_fixes.required` エントリとして記録し、`result: applied` かつ `evidence` 完備のものだけが `impl_ready` に貢献する。


## Termination Report Render Flow（#656 規約）

終了レポートの生成は `render_termination_report.py` が担い、以下のフローに従う。

### TERMINATION_REPORT_INPUT_V1

```yaml
TERMINATION_REPORT_INPUT_V1:
  termination_reason: approved | human_escalation | superseded_by_decision  # 必須
  termination_cause: needs_fix_at_iteration_limit | max_iterations_exceeded | human_judgment_required | null  # 任意
  issue_number: <int>       # 任意
  iteration: <int>          # 任意
  blockers_summary: []      # 任意（human_escalation 時に使用）
```

### TERMINATION_REPORT_RENDER_RESULT_V1

```yaml
TERMINATION_REPORT_RENDER_RESULT_V1:
  schema: TERMINATION_REPORT_RENDER_RESULT_V1
  schema_version: 1
  publishable: true | false
  body: <markdown string> | null   # publishable=false のとき null
  reason_code: null | guard_fail_limit_exceeded | invalid_input | internal_error
  termination_reason: approved | human_escalation | superseded_by_decision
  termination_cause: <string> | null
  attempts: 1 | 2
  attempts_log:
    - attempt: 1
      template: normal
      guard_pass: true | false
      errors: []
    - attempt: 2
      template: fallback_minimal
      guard_pass: true | false
      errors: []
  generated_at: <ISO-8601>
```

### Render 試行ルール（dry-run guard 付き）

1. attempt 1: normal template でレポート生成
2. `prose_boundary_policy` 公開 API（`classify_block` / `iter_markdown_blocks`）で dry-run guard を実行
3. guard pass → `publishable=true`, `body=<markdown>` を返す
4. guard fail → attempt 2: fallback minimal template に切り替え
5. attempt 2 guard pass → `publishable=true`, `body=<markdown>` を返す
6. attempt 2 guard fail → `publishable=false`, `body=null`, `reason_code="guard_fail_limit_exceeded"` を返す

**最大試行回数は 2 回**。再生成・LLM・ask・network・gh command を呼ばない。

### termination_reason と termination_cause の分離

| フィールド | 値 | 説明 |
|---|---|---|
| `termination_reason` | `approved` | reviewer が approve し、contract review status: go を確認した |
| `termination_reason` | `human_escalation` | 人間の判断が必要 |
| `termination_reason` | `superseded_by_decision` | 先行決定により Issue が無効化された |
| `termination_cause` | `needs_fix_at_iteration_limit` | needs-fix がイテレーション上限で停止 |
| `termination_cause` | `max_iterations_exceeded` | イテレーション数が max_iterations を超えた |
| `termination_cause` | `human_judgment_required` | human judgment が必要と判定 |
| `termination_cause` | `null` | 原因なし（approved / superseded 等） |

`needs_fix_at_iteration_limit` / `max_iterations_exceeded` は `termination_cause` として扱う。`termination_reason` に設定してはならない。

### GFM Fence Injection 防御（dynamic fence）

- `_make_dynamic_fence(content)`: コンテンツ内の最長バッククォート列 + 1 の長さを持つ fence を生成（最小 3）
- これにより adversarial input（` ``` ` 含む blockers_summary 等）がテンプレート構造を破壊しない

### stderr / stdout 制約

- stdout: machine JSON のみ（`TERMINATION_REPORT_RENDER_RESULT_V1`）
- stderr: diagnostics のみ（guard fail メッセージ・内部エラー等）
- `publishable=false` 時でも stderr にも投稿可能 markdown 本文を出力しない

### callsite integration

本フロー（`render_termination_report.py` の呼び出し）は follow-up Issue に委譲する。
本 policy セクションは renderer ライブラリの規約として機能し、callsite integration 前でも実効性がある。
