---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup

PR マージ後のローカル環境 cleanup と Git 整理を `post-merge-cleanup-worker` SubAgent に委譲して実行する。

## Delegation

main thread は以下の手順で SubAgent に委譲する:

1. `post-merge-cleanup-worker` SubAgent を Agent tool で起動:
   ```
   入力:
     merged_pr_number: <マージした PR 番号>（ステップ 5-6 実行時は必須）
     linked_issue_number: <linked issue 番号、任意>
   ```

2. SubAgent は `POST_MERGE_CLEANUP_REPORT_V1` YAML を返却する

3. main thread が返却された YAML に応じて以下を実行:
   - `human_review_required: true` → 不明事項を人間に判断委ね
   - `follow_up_candidates` あり → `create-issue` / `edit-issue` に委譲して起票
   - `superseded_prs` あり → `gh pr close` / `gh pr comment` を実行
   - `parent_issue_status.recommended_action` あり → `gh issue close` を実行
   - `stash_restored: false` → `stash_entry_ref` を確認、人間判断

## 責務分界

| 責務 | 担当 |
|---|---|
| git / gh 出力分類・cleanup 実行 | SubAgent（fail-close） |
| CONFLICT 検出時の即時停止 | SubAgent |
| follow-up Issue 起票 | main thread（`create-issue` / `edit-issue` 経由）|
| parent issue クローズ実行 | main thread |
| superseded PR close / comment 実行 | main thread |
| 人間判断が必要な事象の最終判断 | 人間 |

## Procedure（SubAgent 側の実行内容）

**実行方針**: 未コミット変更・未追跡ファイルを検出しても、安全に実行できるステップから先行実行し、不明点のみレポートにまとめる。即停止せず main sync / リモート削除済みブランチ削除 / parent issue 確認まで進める。

### 1. 未コミット変更と未追跡ファイルを分類

```bash
uv run python3 .claude/skills/post-merge-cleanup/scripts/classify-git-state.py --format yaml
```

`classify-git-state.py` は `git status --short` / `git stash list` / `git branch -vv` / `git worktree list --porcelain` を subprocess 配列形式で実行し、YAML 構造化出力を返す。

分類結果の読み方:
- **削除可能**: `branches[*].gone == true` のブランチ / 対応 worktree（ステップ 3 で処理）
- **報告対象（削除しない）**: `status.staged` / `status.unstaged` / `status.untracked` に値があるもの
- この時点では削除しない。分類結果はステップ 6 のレポートで返す

### 2. main を origin/main に整合

```bash
STAGED=$(git diff --cached --name-only)
if [ -n "$STAGED" ]; then
  echo "[INFO] staged 変更を一時退避（git stash）"
  git stash
fi
git checkout main
git pull origin main
```

- staged 変更がある場合は必ず `git stash` で退避してから `checkout main`（main に carry over するリスク回避）
- CONFLICT → 即停止し `human_review_required: true` を返す

### 3. worktree / branch を整理

リモート削除済みブランチ（ステップ 1 の classify-git-state.py 出力から `gone: true` を抽出）:
```bash
uv run python3 .claude/skills/post-merge-cleanup/scripts/classify-git-state.py --format json \
  | uv run python3 -c "import json,sys; [print(b['name']) for b in json.load(sys.stdin)['branches'] if b.get('gone')]"
```

**branch 削除条件**:
- リモートが削除済み（`gone`）
- linked issue がクローズ済み（ある場合）

```bash
git branch -d <branch-name>
```

**worktree 削除条件**（worktree 内 staged 変更・未追跡ファイルがないこと）:
```bash
STAGED=$(git -C "<worktree_path>" diff --cached --name-only 2>/dev/null)
UNTRACKED=$(git -C "<worktree_path>" status --short 2>/dev/null | grep -E '^\?\?' || true)
if [ -z "$STAGED" ] && [ -z "$UNTRACKED" ]; then
  git worktree remove <path>
else
  echo "staged/untracked あり: 削除せず報告"
fi
```

削除できないものは `unresolved_cleanup_items` に記録。

### 4. parent issue クローズ条件確認

`merged_pr_number` から linked issue → parent issue を辿り、parent の他 child の状態を確認:

```bash
gh api repos/{owner}/{repo}/issues/{linked_issue}/parent --jq '.number'
gh api repos/{owner}/{repo}/issues/{parent_issue}/sub_issues --jq '.[] | {number, state}'
```

全 child がクローズ済み → `parent_issue_status.recommended_action: close` を返す。**close 実行は main thread**。

### 5. Superseded PR 候補抽出

`merged_pr_number` 未提供時は skip して `unresolved_cleanup_items` に `merged_pr_number not provided, steps 5/6 skipped` を記録。

同じ Issue を Closes する他の OPEN PR を検索:
```bash
gh pr list --search "linked:issue/<linked_issue> is:open" --json number,title,headRefName,url
```

候補を `superseded_prs` に列挙して返す（実行は main thread）。

### 6. Follow-up 候補の収集

merged PR の本文 / コメントから以下を抽出:
- `## Follow-ups Intentionally Deferred` セクション（あれば）
- レビューコメントで follow-up 化が示唆された項目

候補を `follow_up_candidates` に列挙（起票実行は main thread が `create-issue` / `edit-issue` に委譲）。

### 7. Stash の復帰

```bash
git stash list | grep "stash@{" | head -5
```

ステップ 2 で stash した entry があれば `git stash pop` を試行。CONFLICT → 即停止し `human_review_required: true` で返す。

### 8. POST_MERGE_CLEANUP_REPORT_V1 を生成

後述の Output 仕様で YAML を返す。

## Output: POST_MERGE_CLEANUP_REPORT_V1

```yaml
POST_MERGE_CLEANUP_REPORT_V1:
  status: ok | partial | failed
  generated_at: <ISO 8601>
  generated_by: post-merge-cleanup-worker
  human_review_required: true | false
  cleaned_branches: []
  cleaned_worktrees: []
  unresolved_cleanup_items: []
  parent_issue_status:
    parent_issue_number: <int>
    all_children_closed: true | false
    recommended_action: close | keep_open | n/a
  superseded_prs: []
  follow_up_candidates:
    - title: "<候補タイトル>"
      kind: implementation | research
      source: "<出典: PR コメント等>"
      recommended_routing: create-issue | edit-issue
  stash_restored: true | false | n/a
  stash_entry_ref: "<stash@{N} or null>"
  warnings: []
  errors: []
```

## Guardrails

- `merged_pr_number` 未提供で 5-6 を skip した場合は必ず `unresolved_cleanup_items` に記録
- CONFLICT 検出時は即 fail-close（`human_review_required: true`、復旧操作は人間が判断）
- follow-up 起票は SubAgent 内で実行しない（候補列挙のみ）
- parent issue close / superseded PR close は SubAgent 内で実行しない（候補列挙のみ）
- worktree / branch の削除は確定条件を満たすもののみ。曖昧なら `unresolved_cleanup_items` に記録
- **scripts entrypoint 経由統一**: git 状態の分類は必ず `.claude/skills/post-merge-cleanup/scripts/classify-git-state.py` 経由で実行する
- **inline `gh` / `jq` / `grep` / `awk` / heredoc 使用禁止**: ステップ 1 の git 状態分類での inline bash パイプラインは使用しない
- **スクリプトは `subprocess.run([...])` 配列形式のみ**: `shell=True` 禁止

## Related

- `.claude/agents/post-merge-cleanup-worker.md` — 本 skill を実行する SubAgent
- `.claude/skills/create-issue/SKILL.md` — follow-up 起票委譲先
- `.claude/skills/edit-issue/SKILL.md` — 既存 Issue 更新委譲先
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界
