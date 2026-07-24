---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup / マージ後クリーンアップ

PR マージ後のローカル環境 cleanup と Git 整理を `post-merge-cleanup-worker` SubAgent に委譲して実行する。

Codex CLI では、このステップ専用の custom agent `post-merge-cleanup-worker` を起動する。root thread は直接ファイル編集・テスト実行・commit・push・review judgment を行わない。

## Delegation / 委譲

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
uv run --locked python3 .claude/skills/post-merge-cleanup/scripts/classify-git-state.py --format yaml
```

`classify-git-state.py` は `git status --short` / `git stash list` / `git branch -vv` / `git worktree list --porcelain` を subprocess 配列形式で実行し、YAML 構造化出力を返す。`--format yaml`（デフォルト）と `--format json` のどちらでも `scripts/agent-ops/temp_residue_classifier.py`（Issue #1417）の read-only 出力を `temp_residue_classification` field として含む（`temp_residue_classification/v1`。classifier 実行自体が失敗した場合は `null` を返し、entries が空の成功結果と明確に区別する。`null` を成功として扱ってはならない）。

分類結果の読み方:
- **削除可能**: `branches[*].gone == true` のブランチ / 対応 worktree（ステップ 3 で処理）
- **報告対象（削除しない）**: `status.staged` / `status.unstaged` / `status.untracked` に値があるもの、および `temp_residue_classification.entries[*]` のうち `recommendation: report_only` のもの
- **削除候補として実削除 executor に引き継げる可能性がある**: `temp_residue_classification.entries[*]` のうち `recommendation: eligible_for_delete` のもの。ただしこれは advisory であり、`temp_residue_classifier.py` 自体は削除を実行しない。この Skill / SubAgent も削除を実行しない
- この時点では削除しない。分類結果はステップ 6 のレポートで返す

#### TEMP_CLEANUP_SAFETY_RULES_V1

**現在の本 Skill / SubAgent の authority は read-only advisory のみである。** `temp_residue_classifier.py` の
`recommendation: eligible_for_delete` は marker が valid であっても deletion authorization ではなく、本
Skill / SubAgent はこのセクションのいかなる項目についても filesystem からの削除を一切実行しない
（Issue #1417 PR #1427 review — marker を deletion authority に昇格させない）。

```yaml
TEMP_CLEANUP_SAFETY_RULES_V1:
  never_delete:
    - "tmp/"
    - ".claude/tmp/"
    - ".claude/worktrees/"
  may_delete_without_human: []
  advisory_candidates_for_future_executor_recheck:
    - "owned session subdirectory under tmp/ or .claude/tmp/ only when ownership marker matches — advisory only; NOT an authorization for this Skill/SubAgent or any current executor to delete"
  current_skill_authority:
    temp_residue: report_only
  root_temporary_residue:
    cleanup_required:
      - ".tmp/"
      - ".temp/"
      - ".tmp-*/"
    report_only:
      - "marker 不明の .tmp/**"
      - "marker 不明の .temp/**"
      - "marker 不明の .tmp-*/**"
      - "denied alias（.tmp/ .temp/ .tmp-*/）配下は valid marker があっても常に report_only_unconditionally（初期実装のポリシー。Issue #1417）"
  required_checks:
    - "relative path only"
    - "repo-relative path under an approved root, resolved via dir-fd chain (not pathname-based Path.resolve)"
    - "git ls-files / git status confirms untracked before any future executor considers deletion"
```

- `root temporary residue` は `scripts/agent-ops/temp_residue_classifier.py` が `temp_residue_classification/v1` として read-only 分類する（Issue #1417）。分類は `report_only` または `eligible_for_delete` の `recommendation` を返すのみで、filesystem mutation は一切行わない。
- `tmp/`、`.claude/tmp/`、`.claude/worktrees/` の root 全体削除は自動実行対象にしない。
- `eligible_for_delete` は「実削除 executor が削除直前に再検査してよい候補」を意味する advisory であり、classifier の serialized 出力単体を deletion authorization として扱ってはならない。ownership marker が valid であることも同様に deletion authorization ではない（accidental-isolation モデルの advisory hint に過ぎない）。実削除 executor（marker replay 防止・dir-fd I/O・postcondition 検証を含む）は本 Skill の scope 外であり、必要になった時点で別 Issue として設計する。それまでの間、本 Skill / SubAgent は `temp_residue_classification` の内容に関わらず一切の削除を実行しない。

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
uv run --locked python3 .claude/skills/post-merge-cleanup/scripts/classify-git-state.py --format json \
  | uv run python3 -c "import json,sys; [print(b['name']) for b in json.load(sys.stdin)['branches'] if b.get('gone')]"
```

**worktree / branch 削除フロー（Issue #1137: cleanup_exec 認可境界）**:

agent は bare `git -C <worktree>` で clean 判定や `git worktree remove` / `git branch -d` を直接実行しない。
clean 判定・PR merged / head branch / linked issue / catalog / branch / root=default の検証・削除は、
単一の認可境界 `scripts/agent-ops/cleanup_exec.py` が実行のたびに内部で行う（agent からの bare git cleanup は
`worktree_scope_guard` が deny する）。

1. guard arbitration を機械判定する（mutation を行わない・`AGENT_GUARD_PREFLIGHT_V1` を返す）:
```bash
uv run --locked python3 scripts/agent-ops/guard_preflight.py --json
```
`status: ok` 以外（`blocked` / `human_required`）は `allowed_next_commands` の構造化 recovery hint に従う。
`root_drift_active_worktree_mismatch` は policy B により自動 mutation せず人間承認を要する。

2. 認可境界 `cleanup_exec` で worktree / branch を削除する（PR merged 等を毎回検証してから exact 削除）:
```bash
uv run --locked python3 scripts/agent-ops/cleanup_exec.py \
  --pr-number <pr> --linked-issue-number <issue> \
  --worktree-path <絶対 worktree path> --branch-name <branch> --json
```
`status: ok` で `actions_taken` に `worktree_remove` / `branch_delete` が入る。`status: refused` の場合は
`reason_code`（`pr_not_merged` / `worktree_dirty` / `root_not_default_branch` 等）を `unresolved_cleanup_items` に記録する。

cleanup の正本経路は `cleanup_exec` **のみ** とする（Issue #1137 Blocker 4）。`cleanup_exec` は worktree
remove と branch delete を **単一トランザクション** として内部で行う。agent が bare `git worktree remove` →
`git branch -d` を別々に発行する経路は採用しない。理由: 単一の one-shot V3 contract path では 2 操作分を
同時に保持できず、先に worktree を remove すると次の branch-delete 契約が「worktree が catalog にない」ため
materialize 不能になり、bare-git ルートは `--no-verify` 無しでは完遂できないため（運用事故を誘発する）。

`materialize_cleanup_contract.py` / `worktree_scope_guard` の V3 one-shot gate（`command_hash` / `expires_at` /
`operation` / claim-first consume + tombstone）は agent 向けの cleanup 経路ではなく、guard 層の
**defense-in-depth** として残す内部機構である。post-merge-cleanup skill は bare git cleanup を案内しない。

通常 cleanup で `worktree_remove` が成功した後に内部 `git branch -d` が ancestry 理由で失敗した場合、
`cleanup_exec` は**同じ cleanup_exec invocation 内だけで**既存 branch-only authorization を再実行する。
この same cleanup_exec invocation は新しい agent-facing cleanup command を追加しない。
merged PR・PR head branch・local branch tip/head OID（または限定 squash equivalence）・default base・
same-repository・linked issue・worktree disk/catalog 不在・他 worktree 未使用の全条件を再確認できた場合のみ、
executor 内部の subprocess array で `git branch -D` を使う。再認可が拒否された場合も既に完了した
`worktree_remove` は `actions_taken` に保持する。agent は bare または wrapper 経由の force delete を実行しない。

削除できないものは `unresolved_cleanup_items` に記録する。

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

### 5a. merged PR の pr_review.publish marker を限定 archive する（Issue #1602）

`merged_pr_number` が提供されている場合、当該 PR の
`artifacts/<pr>/issue-metadata/pr_review.publish/pr_review_publish.marker.json`
（`PR_REVIEW_PUBLISH_MARKER_V1`。存在しない場合は何もしない）を
`pr_review_marker_archive_exec.py` に限定して引き渡す。この executor だけが、
merged PR number と exact marker path を明示した呼び出しから、remote readback
（merged-check endpoint + `review_id` primary key の exact review 一致）を経て
marker を repo 外 archive root へ退避し、repo 側 marker を除去する権限を持つ
（詳細契約: `docs/dev/agent-skill-boundaries.md` の
「PR Review Marker Archive Lifecycle」セクション）。

```bash
uv run --locked python3 scripts/agent-ops/pr_review_marker_archive_exec.py \
  --pr-number <merged_pr_number> --json
```

`archived` / `already_archived` は成功として扱い、追加のアクションを要求しない。
`source_retained` / `indeterminate` / `refused` / `environment_blocked` は
`unresolved_cleanup_items` に `PR_REVIEW_MARKER_ARCHIVE_RESULT_V1.status` /
`reason_code` を記録する（SubAgent はこれらの状態を自動リトライ・強制削除しない）。
本 executor は marker 以外の artifact family には一切適用しない。

### 6. Follow-up 候補の収集

merged PR の本文 / コメントから以下を抽出:
- `## Follow-ups Intentionally Deferred` セクション（あれば）
- レビューコメントで follow-up 化が示唆された項目

候補を `follow_up_issue_requests` に `FOLLOW_UP_ISSUE_REQUEST_V1` 形式で列挙する（起票実行は main thread が `issue-author` SubAgent / `create-issue` 経由で実行）。

### 6a. Delivery-rollup Parent の残り child 検出（追加ステップ）

ステップ 4 で取得した parent issue が `parent_mode: delivery-rollup` の場合、`plan_child_materialization.py` を実行して残り child を検出し `follow_up_issue_requests` に追加する。

```bash
# parent が delivery-rollup かどうか確認
PARENT_BODY=$(gh issue view "$PARENT_ISSUE_NUM" --json body --jq '.body')
PARENT_MODE=$(echo "$PARENT_BODY" | grep -oP 'parent_mode:\s*\K[\w-]+' | head -1)

if [ "$PARENT_MODE" = "delivery-rollup" ]; then
  # read-only plan を取得
  uv run --locked python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
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

## Output / 出力: POST_MERGE_CLEANUP_REPORT_V1

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
  pr_review_marker_archive:
    schema: PR_REVIEW_MARKER_ARCHIVE_RESULT_V1
    status: archived | already_archived | source_retained | indeterminate | refused | environment_blocked | n/a
    reason_code: null
    pr_number: <int>
    source_relpath: "artifacts/<pr>/issue-metadata/pr_review.publish/pr_review_publish.marker.json"
    marker_sha256: null
    archive_locator: null
    archive_durable: false
    source_present_after: "unknown"
    source_directory_synced: false
    remote: {}
  stash_restored: true | false | n/a
  stash_entry_ref: "<stash@{N} or null>"
  warnings: []
  errors: []
```

## Probe Scripts for Read-Only Git Operations / 読み取り専用 Git probe script

複雑な git read-only probe（branch/ref 確認・worktree catalog 取得）は以下の script を優先する:

```bash
# branch/ref の read-only probe (git for-each-ref の代替)
uv run --locked python3 scripts/agent-ops/git_ref_probe.py --branch <branch> --json

# worktree catalog の read-only probe (git worktree list --porcelain の代替)
uv run --locked python3 scripts/agent-ops/git_worktree_probe.py --json
```

raw `git for-each-ref` や raw `git worktree list --porcelain` の shell 使用例は
これらの probe script に置き換えることで shell quoting / compound command の迷走を回避する。

## Guardrails / ガードレール

- `merged_pr_number` 未提供で 5-6 を skip した場合は必ず `unresolved_cleanup_items` に記録
- CONFLICT 検出時は即 fail-close（`human_review_required: true`、復旧操作は人間が判断）
- follow-up 起票は SubAgent 内で実行しない（候補列挙のみ）
- parent issue close / superseded PR close は SubAgent 内で実行しない（候補列挙のみ）
- worktree / branch の削除は確定条件を満たすもののみ。曖昧なら `unresolved_cleanup_items` に記録
- **scripts entrypoint 経由統一**: git 状態の分類は必ず `.claude/skills/post-merge-cleanup/scripts/classify-git-state.py` 経由で実行する
- **inline `gh` / `jq` / `grep` / `awk` / heredoc 使用禁止**: ステップ 1 の git 状態分類での inline bash パイプラインは使用しない
- **スクリプトは `subprocess.run([...])` 配列形式のみ**: `shell=True` 禁止
- **root temporary residue の削除実行禁止**: `temp_residue_classification` の `recommendation: eligible_for_delete` を見ても、本 Skill / SubAgent は削除を実行しない（read-only classifier の出力を消費するのみ）

## Related / 関連

- `.claude/agents/post-merge-cleanup-worker.md` — 本 skill を実行する SubAgent
- `.claude/skills/create-issue/SKILL.md` — follow-up 起票委譲先
- `scripts/agent-ops/temp_residue_classifier.py` — root temporary residue の read-only classifier（Issue #1417）
- `scripts/agent-ops/temp_residue_marker.py` — `temp_residue_owner/v1` ownership marker parser（Issue #1417）
- `schemas/temp_residue_classification_v1.schema.json` / `schemas/temp_residue_owner_v1.schema.json`
- `docs/dev/repository-folder-policy.md` — folder class / cleanup authority の正本
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

clean 判定（`status --porcelain=v1 -z` が空であること）は `cleanup_exec` が **内部で** 行う。
agent が `git -C <worktree_path> status ...` を直接実行する手順は廃止した（Issue #1137 AC14）。
worktree が dirty の場合 `cleanup_exec` は `status: refused` / `reason_code: worktree_dirty` を返すので、
`unresolved_cleanup_items` に記録する。


### 注意: cleanup grammar（guard 内部の defense-in-depth gate）

`worktree_scope_guard` の cleanup 判定（`git worktree remove <path>` / `git branch -d <branch>` の
**bare 形式のみ** 許可、`-C` 付き等は deny）は、guard 層の defense-in-depth であり agent 向けの
cleanup 経路ではない。post-merge-cleanup skill の cleanup は `cleanup_exec` 経由のみで発行し、
agent が bare 形式の `git worktree remove` / `git branch -d` を直接発行する手順は採用しない。

### worktree_path の制約

`worktree_path` は `<project_root>/.claude/worktrees/` の直下のパスである必要があります。
それ以外のパス（project root 自体、任意のファイルシステムパス等）は deny されます。
