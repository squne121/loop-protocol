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

不一致なら停止し、人間判断を仰ぐ。blocker / dependency の close 状態が primary signal であり、ラベルの有無は ready 判定に影響しない（`docs/dev/github-ops.md` 参照）。

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
