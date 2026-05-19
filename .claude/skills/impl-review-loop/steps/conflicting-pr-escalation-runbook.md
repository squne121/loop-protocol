# CONFLICTING PR Escalation Runbook

`TEST_VERDICT.mergeable: CONFLICTING` または `merge_state_status: DIRTY` 検出時の対応手順。

## 検出条件

- test-runner SubAgent が `gh pr view --json mergeable,mergeStateStatus` で `CONFLICTING` / `DIRTY` を返した
- または pr-reviewer の LOOP_VERDICT で `mergeable: CONFLICTING` が記録された

## エスカレーション手順

### 1. 状況の確認

```bash
PR_NUMBER=<LOOP_STATE.pr_number>
BRANCH=<LOOP_STATE.branch>
WORKTREE=<LOOP_STATE.worktree>

cd "$WORKTREE"
git fetch origin main
git log --oneline HEAD..origin/main | head -10  # main 側で進んだコミット
```

### 2. implementation-worker への resolve 委譲

```
subagent_type: implementation-worker
inputs:
  task: conflict_resolve
  pr_number: <PR_NUMBER>
  worktree: <WORKTREE>
  branch: <BRANCH>
  conflict_origin: "main が <N> コミット先行"
```

implementation-worker は worktree 内で:
1. `git fetch origin main`
2. `git merge origin/main` または `git rebase origin/main`（プロジェクト方針に従う）
3. conflict ファイルを解決
4. `git add` で解決済みファイルをステージング
5. merge コミット or rebase 続行
6. `git push --force-with-lease origin <BRANCH>`（rebase した場合）または `git push`（merge した場合）

`--force` は使わない（`--force-with-lease` で並行 push 衝突を防ぐ）。

### 3. resolve 後の再検証

resolve 完了後、orchestrator は Step 2（Verification）を再委譲し、新しい head に対する TEST_VERDICT を取得する。

### 4. 連続 conflict の処理

同一イテレーション内で 2 回連続 conflict が発生した場合（resolve しても再度 CONFLICTING）:

- LOOP_STATE.blockers_history に "consecutive conflicts on iteration <N>" を記録
- `termination_reason: human_escalation` を立てて停止
- Issue コメントで人間判断を仰ぐ:

```bash
gh issue comment <issue_number> --body "## impl-review-loop: 連続 CONFLICTING 検出 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <N>
- 直近 conflict log: <git log --oneline HEAD..origin/main の最終出力>
- 人間判断を仰ぎます: main の競合変更が想定外。本 PR の方針を再評価してください"
```

## Guardrails

- `--force` は使わず必ず `--force-with-lease`
- 連続 conflict は 2 回までで自動 escalation（無限ループ防止）
- conflict 解決自体は orchestrator から行わず、implementation-worker SubAgent に委譲する（data-plane 操作の単一委譲先）
