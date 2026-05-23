# Preparation（事前準備）

ループ開始前に LOOP_STATE を初期化し、必要な前提を確認する。

## 1. Inputs の確認

```yaml
issue_number: <int, 必須>
contract_snapshot_url: <URL, 必須>
max_iterations: 5  # 任意
```

- `contract_snapshot_url` のコメントを `gh api` で取得し、`status: go` 判定が記録されていることを確認
- 不一致の場合は `issue-contract-review` を先に通すよう人間に提案して停止

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

# issues と PRs の一覧を取得（open のみ）
gh issue list --repo "$REPO_FULL_NAME" --state open --json number,title,body,labels > /tmp/issues_open.json
gh pr list --repo "$REPO_FULL_NAME" --state open --json number,title,body,labels > /tmp/prs_open.json

# scope rollup preflight を実行（read-only — mutation なし）
python3 .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py \
  --issues-json /tmp/issues_open.json \
  --prs-json /tmp/prs_open.json \
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
