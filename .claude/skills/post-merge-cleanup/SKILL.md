---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup / マージ後クリーンアップ

PR マージ後のローカル環境 cleanup と Git 整理を `post-merge-cleanup-worker` SubAgent に委譲して実行する。

Codex CLI では、このステップ専用の custom agent `post-merge-cleanup-worker` を起動する。root thread は直接ファイル編集・テスト実行・commit・push・review judgment を行わない。

## Delegation / 委譲

main thread は以下の static call shape で SubAgent に委譲する:

```yaml
spawn_agent:
  task_name: post_merge_cleanup_pr{merged_pr_number}_i{attempt}
  agent_type: post-merge-cleanup-worker
  fork_turns: none
  message: |
    Objective: classify and perform the bounded post-merge cleanup contract for the actual merged PR.
    Live reference: bind the actual merged PR number and linked Issue number.
    Bounded scope: bind the canonical cleanup scripts, actual worktree, actual branch, and follow-up candidates.
    Expected result: POST_MERGE_CLEANUP_REPORT_V1 with cleanup and human-review facts.
```

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の merged PR number と非負 attempt で `post_merge_cleanup_pr{merged_pr_number}_i{attempt}` から materialize する。たとえば固定の PR 番号を用いず、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の merged PR number、linked Issue number、worktree path、branch name、canonical cleanup scripts、follow-up candidates を値として埋め込む。`merged PR number` の自然言語参照、変数名、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

完了の扱いは4 site 共通の [Common Completion Protocol](../impl-review-loop/steps/step-4-pr-review.md#common-completion-protocol) に従う。

1. `post-merge-cleanup-worker` SubAgent を Agent tool で起動する。

2. SubAgent は `POST_MERGE_CLEANUP_REPORT_V1` YAML を返却する

3. main thread が返却された YAML に応じて以下を実行:
   - `human_review_required: true` → 不明事項を人間に判断委ね
   - `follow_up_issue_requests` あり → main thread が **即時** `issue-author` SubAgent に委譲して `create-issue` 経由で自動起票する（dedupe_key ベースで重複チェック。SubAgent 内では起票しない。候補列挙のみ）
   - `superseded_prs` あり → `gh pr close` / `gh pr comment` を実行
   - `parent_issue_status.recommended_action == "close"` かつ `parent_issue_status.all_children_closed == true` かつ `parent_issue_status.parent_issue_number` が正の整数（1 以上）のときに限り `gh issue close` を実行する。`recommended_action` は必須フィールドであるため単純な非 null 判定（「あり」）で close してはならない。`recommended_action` が `keep_open` または `n/a` の場合、`all_children_closed` が `false` の場合、または `parent_issue_number` が正の整数でない場合は `gh issue close` を実行しない
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

## Executor 手順の参照（instruction boundary — Issue #1733）

worker が実行する mechanical executor procedure（8 ステップの deterministic cleanup commands、
git/gh 結果分類、worktree/branch binding 検証、cleanup failure taxonomy、
`POST_MERGE_CLEANUP_REPORT_V1` の生成）は本 orchestrator Skill の本文に保持しない。

worker（`post-merge-cleanup-worker`）は Claude Code では `.claude/agents/post-merge-cleanup-worker.md`
の `skills: [post-merge-cleanup-executor]` frontmatter 経由、Codex CLI では
`.codex/agents/post-merge-cleanup-worker.toml` の `repo_local_skill_surface` 経由で
`post-merge-cleanup-executor` Skill（canonical body: `.claude/skills/post-merge-cleanup-executor/SKILL.md`、
Codex thin wrapper: `.agents/skills/post-merge-cleanup-executor/SKILL.md`）を参照し、
本 orchestrator の main-thread 向け routing instruction（worker 起動、follow-up 起票実行、
parent close 実行、superseded PR close 実行）を読み込まない。

orchestrator（本 Skill）が知っておくべき worker 出力の要点（`POST_MERGE_CLEANUP_REPORT_V1` の
routing に使うフィールドのみ）は上記「main thread が返却された YAML に応じて以下を実行」節に
列挙済みである。フィールドの完全な型定義・生成手順・validator は
`post-merge-cleanup-executor` Skill 側の Output セクションおよび
`scripts/check_post_merge_cleanup_boundary.py` を正本とする。

executor（`post-merge-cleanup-executor` Skill）は branch / worktree の状態確認に
`scripts/agent-ops/git_ref_probe.py` と `scripts/agent-ops/git_worktree_probe.py` を使う
（raw `git for-each-ref` / raw `git worktree list --porcelain` を直接呼ばない）。
probe script の呼び出し手順そのものは executor 側の canonical body を参照し、本 orchestrator には
複製しない。

## Guardrails / ガードレール（orchestrator 側）

- follow-up 起票は main thread（本 orchestrator）でのみ実行する。worker / executor 側は候補列挙のみで `gh issue create` を直接呼び出さない
- parent issue close / superseded PR close の実行は main thread（本 orchestrator）でのみ行う
- worker（`post-merge-cleanup-worker`）を再起動する指示、または nested delegation（`Agent` tool 経由・Bash 経由の外部 agent CLI 起動）を worker に要求しない

## Related / 関連

- `.claude/agents/post-merge-cleanup-worker.md` — 本 skill が起動する SubAgent
- `.claude/skills/post-merge-cleanup-executor/SKILL.md` — worker が実行する mechanical executor procedure（canonical body）
- `.agents/skills/post-merge-cleanup-executor/SKILL.md` — Codex CLI 向け thin wrapper
- `.claude/skills/create-issue/SKILL.md` — follow-up 起票委譲先
- `scripts/check_post_merge_cleanup_boundary.py` — orchestrator/executor 責務境界と `POST_MERGE_CLEANUP_REPORT_V1` の validator
- `docs/dev/repository-folder-policy.md` — folder class / cleanup authority の正本
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`POST_MERGE_CLEANUP_REPORT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
