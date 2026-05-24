# Preparation（事前準備）

ループ開始前に LOOP_STATE を初期化し、必要な前提を確認する。

## 1. Inputs の確認

```yaml
issue_number: <int, 必須>
contract_snapshot_url: <URL, 任意>  # 省略時は以下の自動検出フローで取得
max_iterations: 5  # 任意
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

**ステップ 3: 既存 `status: go` が存在しない場合 — `issue-contract-review` 先行実行**

有効な `status: go` が検出されなかった場合にのみ、`issue-contract-review` を先行実行する。

実行後、生成された最新の `CONTRACT_REVIEW_RESULT_V1` を再取得し、以下で分岐する:

- `status: go` かつ `generated_by`, `issue_url`, `generated_at` が妥当:
  - その comment URL を `contract_snapshot_url` として採用
  - `LOOP_STATE.contract_snapshot_source` に `materialized_by_issue_contract_review` を記録
- `status: blocked`:
  - `contract_snapshot_url` を設定せず停止
  - `LOOP_STATE.termination_reason` に `human_escalation` を記録
- 有効な `CONTRACT_REVIEW_RESULT_V1` が見つからない:
  - 停止し、人間判断を仰ぐ

> **スコープ境界（#245 との関係）**: #245 のプリフライトで `contract_snapshot_url` 未提供問題が再現したため、本 Issue（#149）は contract snapshot materialization の canonical fix として扱う。一方で、#245 で観察された環境固有の ready tuple / 関連調整（#245 は session-recording docs Issue）は本 Issue の対象外であり、別 Issue または #245 側の refinement で扱う。本ステップは contract snapshot の取得（materialization）のみを担う。

### 1-c. contract snapshot 内 VC preflight の参照（#329）

contract snapshot コメント内には `CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight` セクションが含まれており、以下の情報を保持:

- 各 VC の実行結果（exit code / stdout / stderr）
- root-cause 分類（category / decision / confidence）：`baseline_vc_preflight.py` による自動分類
- `status: pass | blocked | human_judgment`

**Step 1 (implementation) で実施すべき確認**:

- contract snapshot の `vc_preflight.status` が `blocked` でないことを確認（blocked なら停止）
- `vc_preflight.classifications` 配列を参照し、各 VC の `decision` を把握
  - `decision: go` → baseline で失敗することが予期される
  - `decision: blocked` → 実行不可能（コマンド不在等）
  - `decision: human_judgment` → 分類不能（実装中に追加注意）
- implementation-worker が `## Verification Commands` を実行する際、preflight 結果を context として活用し、追加注意や検証スキップの判断に用いる

preflight では `baseline_vc_preflight.py` により **script 化された自動分類** が行われるため、本ステップでは重複実行を避ける（idempotency）。

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

## 4. LOOP_STATE 初期化

iteration = 0 で開始:

```yaml
LOOP_STATE:
  issue_number: <int>
  contract_snapshot_url: <URL>
  contract_snapshot_source: provided | detected_existing | materialized_by_issue_contract_review
  iteration: 0
  max_iterations: 5
  worktree: .claude/worktrees/issue-<番号>-<slug>
  branch: worktree-issue-<番号>-<slug>
  last_step: null
  last_loop_verdict: null
  blockers_history: []
  external_research_skip_basis: null
  termination_reason: null
```

## 5. 外部仕様調査スキップ判断（任意）

internal-only 変更（`src/state` / `src/systems` 内の純粋ロジック変更等）なら `external_research_skip_basis` に理由を記録してスキップ。
外部仕様が絡む場合は `gemini-cli-headless-delegation` を先行起動して情報を集める。

## 出力

LOOP_STATE 初期値を会話履歴に明示記録し、Step 1（Implementation）へ進む。
