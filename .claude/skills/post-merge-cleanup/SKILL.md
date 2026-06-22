---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup

PR マージ後のローカル環境 cleanup と Git 整理を `post-merge-cleanup-worker` SubAgent に委譲して実行する。

Codex CLI: spawn the custom agent named post-merge-cleanup-worker for this step; the root thread must not edit files, run tests, commit, push, or make the review judgment directly.

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
   - `follow_up_issue_requests` あり → main thread が **即時** `issue-author` SubAgent に委譲して `create-issue` 経由で自動起票する（dedupe_key ベースで重複チェック。SubAgent 内では起票しない。候補列挙のみ）
   - `superseded_prs` あり → `gh pr close` / `gh pr comment` を実行
   - `parent_issue_status.recommended_action` あり → `gh issue close` を実行
   - `stash_restored: false` → `stash_entry_ref` を確認、人間判断

### follow_up_issue_requests の自動起票フロー

`follow_up_issue_requests` が空でない場合、main thread は SubAgent から YAML を受け取った直後に以下を実行する:

```
for each request in follow_up_issue_requests:
  1. dedupe チェック: dedupe_key で既存 Issue を検索（open / closed すべて対象）
     gh issue list --repo squne121/loop-protocol --state all \
       --search '"<dedupe_key>"' --json number,title,url,state,stateReason,labels
  2. 重複なし → issue-author SubAgent に委譲して create-issue skill 経由で起票
     ※ Issue 本文に ## Source セクション（dedupe_key を含む）を必須で付与
  3. 重複あり（open）→ スキップ（既存 Issue 番号をレポートに記録、status: reused_open）
  4. 重複あり（closed / not_planned）→ 起票せずスキップ（status: skipped_closed_not_planned）
  5. 重複あり（closed / completed）→ 起票せずスキップ（status: skipped_closed_completed）
  6. 重複あり（closed / duplicate）→ 起票せずスキップ（status: skipped_closed_duplicate）
  ※ closed Issue を open に差し戻して再利用する場合は human escalation が必要（自動起票不可）
```

起票・スキップした follow-up Issue の情報を終了コメントの `follow_up_issues` フィールドに列挙する（`FOLLOW_UP_MATERIALIZATION_RESULT_V1` 形式。詳細スキーマは `docs/dev/agent-skill-boundaries.md` 参照）。

終了コメントのテンプレート（`FOLLOW_UP_MATERIALIZATION_RESULT_V1` を含む）:

````markdown
## post-merge-cleanup: 完了 (<timestamp>)

- status: ok | partial | failed
- 次アクション: <親 Issue クローズ / 人間判断 等>

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: post-merge-cleanup
  follow_up_issues:
    - request_dedupe_key: "..."
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
      issue:
        number: 123
        url: "https://github.com/..."
      reason: null

  note_only_observations:
    - dedupe_key: "..."
      source_url: "..."
      source_note_id: "..."
      summary: "..."
```
````

## 責務分界

| 責務 | 担当 |
|---|---|
| git / gh 出力分類・cleanup 実行 | SubAgent（fail-close） |
| CONFLICT 検出時の即時停止 | SubAgent |
| follow-up Issue 起票 | main thread（`create-issue` 経由。dedupe ヒット時はスキップ）|
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

**worktree 削除フロー（Issue #1137: guard arbitration + V3 cleanup contract）**:

agent が直接 `git -C <worktree>` で clean 判定を行わない。clean 判定（staged / unstaged / untracked の有無）は
`worktree_scope_guard` の内部 subprocess 配列実行（`git -C <expected_path> status --porcelain=v1 -z`）へ集約され、
guard 同士の arbitration 状態は `guard_preflight.py` が事前に機械判定する。

1. guard arbitration preflight（mutation を行わない）:
```bash
uv run python3 scripts/agent-ops/guard_preflight.py --json
```
`status: ok` 以外（`blocked` / `human_required`）の場合は `allowed_next_commands` の構造化 recovery hint に従う。
`root_drift_active_worktree_mismatch` は policy B により自動 mutation せず人間承認を必要とする。

2. cleanup contract を safe scratch path へ materialize（env-prefix / `.claude/artifacts` 非依存）:
```bash
uv run python3 scripts/agent-ops/materialize_cleanup_contract.py \
  --pr-number <pr> --linked-issue-number <issue> \
  --worktree-path <絶対 worktree path> --branch-name <branch> --json
```
`artifacts/agent-ops/cleanup_contract.json`（gitignored / 期限付き `expires_at` / `command_hash` 付き）を生成する。

3. gated cleanup を実行する（clean 判定は guard 内部で実施される）:
```bash
git worktree remove <path>
git branch -d <branch-name>
```
`worktree_scope_guard` が V3 contract を検証し、`expires_at` 期限切れ（`cleanup_contract_expired`）・
`command_hash` 不一致（`cleanup_command_hash_mismatch`）・worktree dirty（`worktree_dirty`）を block する。
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

候補を `follow_up_issue_requests` に `FOLLOW_UP_ISSUE_REQUEST_V1` 形式で列挙する（起票実行は main thread が `issue-author` SubAgent / `create-issue` 経由で実行）。

#### 6a. Delivery-rollup Parent の残り child 検出（追加ステップ）

ステップ 4 で取得した parent issue が `parent_mode: delivery-rollup` の場合、`plan_child_materialization.py` を実行して残り child を検出し `follow_up_issue_requests` に追加する。

```bash
# parent が delivery-rollup かどうか確認
PARENT_BODY=$(gh issue view "$PARENT_ISSUE_NUM" --json body --jq '.body')
PARENT_MODE=$(echo "$PARENT_BODY" | grep -oP 'parent_mode:\s*\K[\w-]+' | head -1)

if [ "$PARENT_MODE" = "delivery-rollup" ]; then
  # read-only plan を取得
  uv run python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
    --repo <owner>/<repo> \
    --issue "$PARENT_ISSUE_NUM"
fi
```

`CHILD_MATERIALIZATION_PLAN_V2.children` の各エントリを処理する:

| action | 処理 |
|---|---|
| `create_issue` | `severity: optional_follow_up` の `FOLLOW_UP_ISSUE_REQUEST_V1` を生成（起票実行は main thread） |
| `reuse_and_update_parent` | parent body 更新を `follow_up_issue_requests` に追加（main thread が `edit-issue` skill に委譲） |
| `no_op` | スキップ |
| `human_escalation` | `warnings` に記録し `human_review_required: true` で返す |

`FOLLOW_UP_ISSUE_REQUEST_V1` の `dedupe_key` は `CHILD_MATERIALIZATION_PLAN_V2.children[*].dedupe_key` を使用する。
スキーマ正本: `docs/dev/agent-skill-boundaries.md#CHILD_MATERIALIZATION_PLAN_V2`

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
  follow_up_issue_requests:
    - title: "..."
      issue_kind: implementation
      severity: optional_follow_up
      source:
        kind: post_merge_cleanup
        url: "https://github.com/..."
        note_id: "1"
      dedupe_key: "follow-up:squne121/loop-protocol:pr/<PR番号>:1"
      desired_destination: "..."
      validated_scope_delta: "..."
      origin_skill: post-merge-cleanup
      labels:
        - triage-required
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
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`POST_MERGE_CLEANUP_REPORT_V1` の全フィールドは必ず含める（routing 必須フィールド）。


## POST_MERGE_CLEANUP_REQUEST_V2 スキーマ

worktree_scope_guard の cleanup contract として使用する JSON スキーマ。
供給元: main thread / post-merge-cleanup skill が `CLAUDE_WORKTREE_CLEANUP_CONTRACT`
環境変数に JSON を渡す（優先）、または hook が `.claude/artifacts/cleanup_contract.json` を読む。

```json
{
  "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
  "worktree_path": "/abs/path/to/.claude/worktrees/issue-N-slug",
  "branch_name": "worktree-issue-N-slug",
  "require_clean": true
}
```

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` | `"POST_MERGE_CLEANUP_REQUEST_V2"` | スキーマ識別子（固定） |
| `worktree_path` | `string` | 削除対象の worktree の絶対パス |
| `branch_name` | `string` | `git branch -d` で削除するブランチ名 |
| `require_clean` | `bool` | `true` の場合、`git -C <path> status --porcelain=v1 -z` が空であることを確認してから削除 |

### worktree 削除条件（require_clean チェック）

`require_clean: true` の場合、`git -C <worktree_path> status --porcelain=v1 -z` の出力が
空であること（unstaged modification を含む全変更がないこと）を確認してから worktree を削除する。
出力が空でない場合は cleanup を中断し `unresolved_cleanup_items` に記録する。


### 注意: cleanup grammar の制限（`git -C` 禁止）

`worktree_scope_guard` の cleanup 判定では、`git worktree remove <path>` および
`git branch -d <branch>` の **bare 形式のみ** を許可します。
`git -C <path> worktree remove <target>` のような `-C` フラグ付きの形式は
cleanup decider が `not_a_cleanup_command` で deny します。

post-merge-cleanup skill が cleanup 操作を発行する際は、bare 形式のコマンドのみを使用してください。

### worktree_path の制約

`worktree_path` は `<project_root>/.claude/worktrees/` の直下のパスである必要があります。
それ以外のパス（project root 自体、任意のファイルシステムパス等）は deny されます。
