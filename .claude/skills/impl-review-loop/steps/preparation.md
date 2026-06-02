# Preparation（事前準備）

ループ開始前に LOOP_STATE を初期化し、必要な前提を確認する。

## 0. Intake Gate — `CONTRACT_REVIEW_RESULT_V1 status: go` 必須検査

`impl-review-loop` preparation の最初のゲート。以下の 5 つのサブ理由を **優先順位の高い順**に評価し、いずれかに該当する場合は `intake_gate_failed` として停止する。後続ステップへは進まない。

```yaml
INTAKE_GATE_RESULT_V1:
  status: pass | intake_gate_failed
  subreason: null | metadata_not_ready | missing_contract_go | stale_contract_review | body_snapshot_mismatch | request_changes_after_go
  detail: "<人間向け説明>"
```

### サブ理由の優先順位と判定条件

評価は以下の順序で行い、最初に該当したサブ理由で停止する（複数該当しても最初の 1 つを返す）:

#### 1. `metadata_not_ready`（最高優先）

Issue の routing metadata が `impl-review-loop` の前提を満たさない場合。以下のいずれかが欠落:

- **title prefix** が `実装:` または `implement:` で始まっていない
- **`phase/implementation` label** が付与されていない

```bash
gh issue view <issue_number> --json title,labels \
  --jq '{title: .title, labels: [.labels[].name]}'
```

どちらか一方でも欠落していれば `intake_gate_failed: metadata_not_ready` で停止。

#### 2. `missing_contract_go`

`contract_snapshot_url` が提供されておらず、Issue コメントにも有効な `CONTRACT_REVIEW_RESULT_V1 status: go` が存在しない場合。

- `contract_snapshot_url` 未提供 → Issue コメントを自動検出しても `status: go` の valid block が見つからない
- この場合は **`issue-contract-review` を自動実行しない**（fail-only gate）。`intake_gate_failed: missing_contract_go` で停止し、人間に `issue-contract-review` の実行を依頼する

> **設計決定**: #149 実装の自動実行（`status: go` 不在時に `issue-contract-review` を自動呼び出し）は `impl-review-loop` preparation Step 1-b の旧設計。本 Issue（#564）以降は fail-only gate に変更する。自動実行パスは廃止。

#### 3. `stale_contract_review`

`CONTRACT_REVIEW_RESULT_V1.status == "go"` のコメントが存在するが、freshness チェックに失敗した場合:

- `body_sha256` フィールドが go コメントに存在し、かつ現在の Issue body の sha256 と一致しない
- `body_sha256` フィールドが存在しない場合のフォールバック: `CONTRACT_REVIEW_RESULT_V1.generated_at` < Issue の `updated_at`（go コメント生成後に Issue 本文が更新された）

いずれかの条件が真の場合は `intake_gate_failed: stale_contract_review` で停止し、`issue-contract-review` の再実行を人間に依頼する。

#### 4. `body_snapshot_mismatch`

`contract_snapshot_url` が明示的に提供され、かつ上記 freshness チェックで body_sha256 または generated_at の不一致が検出された場合。  
（ステップ 3 の freshness チェックがコメント自動検出時に対応し、本サブ理由は明示提供 URL が stale な場合に使用）

#### 5. `request_changes_after_go`（最低優先）

`status: go` のコメントより新しい `CONTRACT_REVIEW_RESULT_V1.status: blocked` または明示的な go 無効化 marker が存在する場合。

**go 無効化 marker の定義**（machine-readable policy block）:

```yaml
GO_INVALIDATION_POLICY_V1:
  source: issue_comment
  accepted_marker: fenced_yaml REVIEW_RESULT_V1.status == request_changes
  target_issue_url_must_match: true
  ordering_key: created_at
  precedence: latest_valid_marker_after_latest_valid_go
```

上記ポリシーに従い、Issue コメントに `REVIEW_RESULT_V1.status == request_changes` を含む fenced yaml block が存在し、かつ対象 Issue URL が一致し、`created_at` が直近の `status: go` より新しい場合は `intake_gate_failed: request_changes_after_go` で停止。

### Stop 時の必須処理（`on_intake_gate_failed`）

`intake_gate_failed` に該当した場合、以下の処理を行ってから停止する:

```yaml
on_intake_gate_failed:
  set LOOP_STATE.termination_reason: intake_gate_failed
  set LOOP_STATE.intake_gate.status: intake_gate_failed
  do_not_continue_to_step_1: true
```

`LOOP_STATE.termination_reason` の有効値: `null | approved | max_iterations | human_escalation | intake_gate_failed`

### 全サブ理由なし → `status: pass`

全ての条件を通過した場合のみ `INTAKE_GATE_RESULT_V1.status: pass` を返し、Step 1 へ進む。

`LOOP_STATE.intake_gate` に結果を記録する:

```yaml
intake_gate:
  status: pass | intake_gate_failed
  subreason: null | metadata_not_ready | missing_contract_go | stale_contract_review | body_snapshot_mismatch | request_changes_after_go
  evaluated_at: "<ISO8601>"
```

## 1. Inputs の確認

```yaml
issue_number: <int, 必須>
contract_snapshot_url: <URL, 任意>  # 省略時は以下の自動検出フローで取得
max_iterations: 3  # 任意
```

### 1-a. `contract_snapshot_url` が提供された場合

`gh api` でコメントを取得し、`CONTRACT_REVIEW_RESULT_V1`・`generated_by: issue-contract-review`・`status: go` の組み合わせが記録されていることを確認する。
確認できない場合は停止し、人間判断を仰ぐ。

`LOOP_STATE.contract_snapshot_source` に `provided` を記録する。

### 1-b. `contract_snapshot_url` が未提供の場合（自動検出フロー）

Issue コメント一覧から `CONTRACT_REVIEW_RESULT_V1` marker を持つ YAML block を検出し、以下の流れで contract_snapshot_url を決定する。

**前提: valid CONTRACT_REVIEW_RESULT_V1 の定義**

YAML block は `CONTRACT_REVIEW_RESULT_V1` marker を含むコメント本文内の fenced code block（``` yaml ... ```）に限定して抽出する。以下の fields がすべて存在し、値が妥当であることを必須とする:

- `status`: `go | blocked` のいずれか
- `generated_by`: `issue-contract-review`
- `issue_url`: 現在の Issue URL と完全一致（例: https://github.com/<owner>/<repo>/issues/<current_issue_number>）
- `generated_at`: ISO8601 形式の timestamp

本文に `CONTRACT_REVIEW_RESULT_V1` / `generated_by: issue-contract-review` / `status: go` が含まれるだけでは採用しない。review comments や example code blocks の引用を誤採用しないこと。

**Issue コメント取得の手順**

```bash
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
ISSUE_NUMBER=<issue_number>

gh api --paginate \
  "repos/${REPO_FULL_NAME}/issues/${ISSUE_NUMBER}/comments?per_page=100" \
  --jq '.[] | select(.body | contains("CONTRACT_REVIEW_RESULT_V1")) |
        {id, html_url, created_at, updated_at, body}'
```

出力は comment `id` 昇順（ascending）で返される。最新判定は `created_at desc, id desc` の precedence で行う（newer が先）。

**ステップ 1: 最新 `status: blocked` チェック**

上記コメント一覧から、valid `CONTRACT_REVIEW_RESULT_V1` YAML block を持つ最新コメント（`created_at desc, id desc`）を特定する。その `status` が `blocked` である場合は、古い `status: go` コメントが存在していても採用せず停止する。

```
最新の valid CONTRACT_REVIEW_RESULT_V1 が status: blocked → 停止（人間判断）
```

**ステップ 2: 既存 `status: go` の検出（idempotency 保証）**

最新の valid `CONTRACT_REVIEW_RESULT_V1` が `status: go` である場合、そのコメント URL を `contract_snapshot_url` として採用する。
既存 `status: go` が存在する場合は、以降の `issue-contract-review` 先行実行をスキップする（idempotency 保証）。

`LOOP_STATE.contract_snapshot_source` に `detected_existing` を記録する。

**ステップ 3: 既存 `status: go` が存在しない場合**

> **廃止（#564 以降）**: 旧設計では有効な `status: go` が検出されなかった場合に `issue-contract-review` を自動実行していた。  
> #564 の intake gate（Step 0）導入後は、この自動実行パスは廃止される。  
> `status: go` が存在しない場合は Step 0 の intake gate で `intake_gate_failed: missing_contract_go` として停止する。  
> Step 1-b のステップ 3 は実行されない。

（後方互換のためセクションを残すが、Step 0 で停止済みのためここには到達しない）

> **スコープ境界（#245 との関係）**: #245 のプリフライトで `contract_snapshot_url` 未提供問題が再現したため、本 Issue（#149）は contract snapshot materialization の canonical fix として扱う。一方で、#245 で観察された環境固有の ready tuple / 関連調整（#245 は session-recording docs Issue）は本 Issue の対象外であり、別 Issue または #245 側の refinement で扱う。本ステップは contract snapshot の取得（materialization）のみを担う。

### 1-c. contract snapshot 内 VC preflight の参照（#329）

contract snapshot コメント内には `CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight` セクションが含まれており、以下の情報を保持:

- 各 VC の実行結果（exit code / stdout / stderr）
- root-cause 分類（category / decision / confidence）：`baseline_vc_preflight.py` による自動分類
- `status: pass | blocked | human_judgment`

**Step 1 (implementation) で実施すべき確認**:

- contract snapshot の `vc_preflight.status` が **`pass` の場合のみ続行**。`blocked` または `human_judgment` の場合は停止し `termination_reason: human_escalation` を記録して人間判断へ送る
- `vc_preflight.classifications` 配列を参照し、各 VC の `decision` を把握
  - `decision: go` → baseline で失敗することが予期される
  - `decision: blocked` → 実行不可能（コマンド不在等） → preflight status: blocked → 停止
  - `decision: human_judgment` → 分類不能 → preflight status: human_judgment → 停止して人間判断へ
- implementation-worker は `pass` 時のみ起動され、preflight 結果を context として活用する

preflight では `baseline_vc_preflight.py` により **script 化された自動分類** が行われるため、本ステップでは重複実行を避ける（idempotency）。

## 1-d. Product Spec Check の評価（#333）

contract snapshot から `CONTRACT_REVIEW_RESULT_V1.checks.product_spec_check` を読み取り、Step 1 delegation 前に決定論的に評価する。以下のスクリプトを実行:

```bash
PRODUCT_SPEC_GATE_DECISION=$(
  python3 .claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py \
    --snapshot-json - \
    --contract-snapshot-url "$CONTRACT_SNAPSHOT_URL" \
    < "$CONTRACT_SNAPSHOT_FILE"
)
```

where:
- `$CONTRACT_SNAPSHOT_FILE`: contract snapshot JSON を持つ一時ファイルのパス
- `$CONTRACT_SNAPSHOT_URL`: contract snapshot comment の GitHub URL（preparation step 1-a / 1-b で検出）

**評価ルール**:

- `routing_action: continue` → Step 1 へ進む
- `routing_action: stop_human` → 停止。`LOOP_STATE.termination_reason: human_escalation` を記録して人間判断へ送る
- `routing_action: refresh_contract_snapshot` → stale / incomplete snapshot として停止。`issue-contract-review` 再実行へ route

**LOOP_STATE への記録**:

評価結果を `LOOP_STATE.product_spec_preflight` に正規化して格納する。Step 1 には raw `product_spec_check` ではなく summary のみを渡す:

```yaml
product_spec_preflight:
  source: contract_snapshot.checks.product_spec_check
  applicability: applicable | not_applicable | missing
  decision: pass | fail | human_judgment | missing
  blocked_rule_ids: []
  contract_snapshot_url: "<url>"
  body_sha256: "<sha256>"
  routing_action: continue | stop_human | refresh_contract_snapshot
```

**注**: `impl-review-loop` は PS001〜PS006 の意味論判定を行わない。evaluation は contract snapshot に既に存在する `product_spec_check` 結果を読むだけ（mutation-free gate）。意味論判定は `issue-contract-review` / `check_product_spec_contract.py` に集約。

## 2. ready tuple の再確認

```bash
gh issue view <issue_number> --json title,labels --jq '.title + " | " + (.labels | map(.name) | join(","))'
```

期待する canonical ready tuple:
- title prefix: `実装:` または `implement:`
- labels: `phase/implementation`
- blocker / dependency: GitHub native dependency（`depends on` リンク）がすべて close 済み、または `Depends on #N` テキスト表現がすべて close 済み（primary signal）

不一致なら停止し、人間判断を仰ぐ。blocker / dependency の close 状態が primary signal であり、state labels の有無は ready 判定に影響しない。ただし `phase/implementation` は issue kind / workflow routing の前提として維持する（`docs/dev/github-ops.md` 参照）。

## 2.5. scope rollup preflight（`plan_issue_scope_rollup.py` 実行）

worktree 作成前に scope rollup preflight を実行し、同一 Allowed Paths / 同一 skill family / 同一 parent_issue / 同一 dedupe_key を持つ OPEN Issue / PR の統合候補を確認する。
preflight は mutation-free（Issue 作成・編集・クローズ禁止）。

```bash
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

# 対象 Issue を個別取得（current_issue として使用）
gh issue view <issue_number> \
  --repo "$REPO_FULL_NAME" \
  --json number,title,body,labels,state,stateReason,url \
  > /tmp/current_issue.json

# issues と PRs の一覧を全状態（open + closed）で取得（デフォルト 30 件制限を回避するため --limit 1000）
gh issue list \
  --repo "$REPO_FULL_NAME" \
  --state all \
  --limit 1000 \
  --json number,title,body,labels,state,stateReason,url \
  > /tmp/issues_all.json

gh pr list \
  --repo "$REPO_FULL_NAME" \
  --state all \
  --limit 1000 \
  --json number,title,body,labels,state,url,files,closingIssuesReferences \
  > /tmp/prs_all.json

# scope rollup preflight を実行（read-only — mutation なし）
python3 .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py \
  --issues-json /tmp/issues_all.json \
  --prs-json /tmp/prs_all.json \
  --current-issue <issue_number> \
  --repo "$REPO_FULL_NAME"
```

出力（`ISSUE_SCOPE_ROLLUP_PLAN_V2`）を `LOOP_STATE.scope_rollup_plan` に格納する。

**orchestrator の判断ルール**:

- `confidence: high` の候補が存在する場合: orchestrator は各候補の `suggested_action` を確認し、統合実施可否を判断してから次ステップに進む。自動実行しない。
- `security` / `auth` / `permission` / `sandbox` 関連の候補（`suggested_action: human_review_required`）: 即時停止して人間が判断する（`termination_reason: human_escalation`）。
- `confidence: medium` の候補: LOOP_STATE に記録し、推奨アクションを提示するが自動実行しない。
- `confidence: low` または候補なし: 記録してそのまま次ステップに進む。

**`ISSUE_SCOPE_ROLLUP_DECISION_V2` の記録**（統合実施・未実施にかかわらず常時記録）:

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<ISSUE_SCOPE_ROLLUP_PLAN_V2.body_sha256>"
    generated_at: "<ISSUE_SCOPE_ROLLUP_PLAN_V2.generated_at>"
  decision: executed | skipped | deferred | human_review_required
  executed_actions: []
  skipped_reason: null
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null
```

`LOOP_STATE.scope_rollup_decision` に記録した後、Step 3（worktree/branch preflight）に進む。
詳細は `.claude/skills/issue-refinement-loop/references/scope-rollup-policy.md` を参照。

## 3. worktree / branch の preflight

```bash
SLUG=$(echo "<title>" | sed 's/.*: //; s/[^a-zA-Z0-9]/-/g; s/--*/-/g; s/^-//; s/-$//' | tr A-Z a-z | cut -c1-40)
WORKTREE=".claude/worktrees/issue-${issue_number}-${SLUG}"
BRANCH="worktree-issue-${issue_number}-${SLUG}"

# 既存衝突確認
git worktree list | grep "$WORKTREE" && echo "[WARN] worktree 既存" || echo "[OK] worktree 未作成"
git branch --list "$BRANCH" && echo "[WARN] branch 既存" || echo "[OK] branch 未作成"
```

既存衝突あり → 過去のイテレーションの残骸の可能性。人間判断を仰ぐ。

## 4. Product Spec Gate 評価完了後の LOOP_STATE 初期化

Product Spec Check 評価（1-d）を完了後、iteration = 0 で以下の状態で開始:

```yaml
LOOP_STATE:
  issue_number: <int>
  contract_snapshot_url: <URL>
  contract_snapshot_source: provided | detected_existing | materialized_by_issue_contract_review
  iteration: 0
  max_iterations: 3
  worktree: .claude/worktrees/issue-<番号>-<slug>
  branch: worktree-issue-<番号>-<slug>
  last_step: null
  last_loop_verdict: null
  blockers_history: []
  external_research_skip_basis: null
  termination_reason: null
  intake_gate:
    status: pass | intake_gate_failed
    subreason: null | metadata_not_ready | missing_contract_go | stale_contract_review | body_snapshot_mismatch | request_changes_after_go
    evaluated_at: "<ISO8601>"
  product_spec_preflight:
    source: contract_snapshot.checks.product_spec_check
    applicability: applicable | not_applicable | missing
    decision: pass | fail | human_judgment | missing
    blocked_rule_ids: []
    contract_snapshot_url: "<url>"
    body_sha256: "<sha256>"
    routing_action: continue | stop_human | refresh_contract_snapshot
```

routing_action が `stop_human` または `refresh_contract_snapshot` の場合は、ループを開始せず人間判断へ escalate する。

## 5. 外部仕様調査スキップ判断（任意）

internal-only 変更（`src/state` / `src/systems` 内の純粋ロジック変更等）なら `external_research_skip_basis` に理由を記録してスキップ。
外部仕様が絡む場合は `gemini-cli-headless-delegation` を先行起動して情報を集める。

## 6. contract blocked 受信時の 3 分類ルーティング（vc_preflight_contract_blocked）

`vc_preflight.status: blocked` を受信したとき、orchestrator は `CONTRACT_BLOCKED_ROUTING_V1` スキーマに従い `contract_blocked_reason` を 3 種に分類し、各クラスの手順に従う。

**重要制約（#569）**:

- classifier logic を新設・改修することは禁止。既存の `vc_preflight.classifications[]` を消費するだけ。routing のみを行う。
- Issue 本文変更は issue-refinement-loop 経由のみ。require_body_sha256_after 付きで呼び出す。
- 本セクションは Step 0 の intake gate を通過済みの状態（`status: go` 受信後）で、Step 1-c の `vc_preflight.status: blocked` 分岐にのみ適用される。

### 6.1 decision table（分類 matrix）

| contract_blocked_reason class | 定義 | autonomous_fix_allowed | required_route |
|---|---|---|---|
| `simple_contract_hygiene_fix` | VC コマンド文字列の typo / 軽微な書式エラー（C4/C9 trivial format に相当）。`vc_preflight.classifications[]` の全分類が `category: format_error` または `category: command_typo` であり、Issue body の意味論変更を要しない | true | issue-refinement-loop（本文変更なし、VC 文字列のみ修正）→ issue-contract-review 再実行 |
| `contract_refinement_required` | VC ロジックの修正や Allowed Paths の追加など、Issue contract の構造変更が必要。`vc_preflight.classifications[]` に `category: scope_gap` / `category: missing_path` / `category: ambiguous_ac` 等が含まれる | false | issue-refinement-loop 経由でスコープ確認 → issue-contract-review 再実行 → 人間承認が必要 |
| `human_decision_required` | 分類不能、または `vc_preflight.classifications[]` に `category: unknown` / `category: security_related` / `category: auth_required` 等が含まれる。autonomous fix 禁止、停止して人間判断 | false（autonomous fix 禁止） | 即停止。`termination_reason: human_escalation` を記録して人間判断を仰ぐ |

### 6.2 CONTRACT_BLOCKED_ROUTING_V1 スキーマ

```yaml
CONTRACT_BLOCKED_ROUTING_V1:
  source_contract_review_comment_url: "<blocked status の issue-contract-review コメント URL>"
  body_sha256_before: "<blocked 受信時点の Issue body sha256>"
  contract_blocked_reason: simple_contract_hygiene_fix | contract_refinement_required | human_decision_required
  autonomous_fix_allowed: true | false
  required_route: "<routing 先の説明>"
  rerun_required: true | false  # issue-contract-review の再実行が必要かどうか
  classification_basis:
    - ac: "<AC ID>"
      category: "<vc_preflight.classifications[].category>"
      decision: "<vc_preflight.classifications[].decision>"
      rationale: "<分類根拠>"
```

### 6.3 各クラスのエスカレーション手順

#### simple_contract_hygiene_fix（自律修正フロー）

1. `vc_preflight.classifications[]` から修正対象の VC コマンド文字列を特定する
2. `issue-refinement-loop` を `require_body_sha256_after: true` 付きで呼び出し、VC 文字列のみを修正する（Issue body の意味論は変更しない）
3. 修正完了後に `issue-contract-review` を再実行する
4. 再実行結果が `status: go` であれば実装フローへ戻る（Step 1 の `vc_preflight.status: pass` 確認）
5. 再実行後も `status: blocked` であれば `contract_refinement_required` または `human_decision_required` に昇格して対応する

**正例（positive example）**:

- `rg "foo" path/to/file` が `path/to/file` のスペルミス（正しくは `path/to/fle`）で blocked → typo fix のみで再実行可
- AC の VC コマンドに余分な引用符やエスケープが含まれ、exit_code: 1 となっている → format_error として修正可

**負例（negative example）**:

- VC が参照する Allowed Path 自体が存在しない → `scope_gap` であり `contract_refinement_required` が必要
- VC の期待値（grep 対象文字列）が実装内容と乖離している → 意味論変更であり `simple_contract_hygiene_fix` 対象外
- `preflight-scope: pr_review_only` フラグが設定された VC → scope class 変更は意味論変更であり autonomous fix 禁止

#### contract_refinement_required（人間承認が必要な修正フロー）

1. `CONTRACT_BLOCKED_ROUTING_V1` を `LOOP_STATE.contract_blocked_routing` に記録する
2. `issue-refinement-loop` を経由して Issue contract の修正を提案する（自律実行は禁止。人間の承認を待つ）
3. 人間承認後に `issue-contract-review` を再実行する
4. 再実行結果が `status: go` であれば実装フローへ戻る

#### human_decision_required（停止・人間判断）

autonomous fix 禁止。即停止して人間判断を仰ぐ。`LOOP_STATE.termination_reason: human_escalation` を記録する。

- `human_decision_required` → 停止。人間が明示的に再開指示を出すまで実装を再開しない
- `category: security_related` / `category: auth_required` の分類が含まれる場合、autonomous fix は絶対に禁止

### 6.4 LOOP_STATE への記録

```yaml
LOOP_STATE:
  contract_blocked_routing:
    evaluated_at: "<ISO8601>"
    schema_version: CONTRACT_BLOCKED_ROUTING_V1
    contract_blocked_reason: simple_contract_hygiene_fix | contract_refinement_required | human_decision_required
    autonomous_fix_allowed: true | false
    rerun_required: true | false
    required_route: "<routing 先>"
```

## 出力

LOOP_STATE 初期値を会話履歴に明示記録し、Step 1（Implementation）へ進む。
