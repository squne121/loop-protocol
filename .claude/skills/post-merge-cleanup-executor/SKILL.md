---
name: post-merge-cleanup-executor
description: PR マージ後の mechanical cleanup executor procedure。git/gh 出力分類・main 整合・worktree/branch 削除（cleanup_exec 経由）・parent issue クローズ条件確認・superseded PR 候補抽出・follow-up 候補収集・POST_MERGE_CLEANUP_REPORT_V1 生成を行う deterministic な 8 ステップ手順。main-thread 向け routing instruction（worker 起動、follow-up 起票実行、parent close 実行、superseded PR close 実行）は一切含まない。`post-merge-cleanup-worker` SubAgent（Claude Code）が `skills:` frontmatter 経由、Codex CLI worker が `repo_local_skill_surface` 経由でこの手順本文を参照する。
---

# Post Merge Cleanup Executor / マージ後クリーンアップ・機械的実行手順

`post-merge-cleanup-worker`（既存の単一 agent。Claude Code / Codex CLI 共通）が実行する
mechanical executor procedure。呼び出し元は `.claude/skills/post-merge-cleanup/SKILL.md`
（top-level orchestrator）であり、本 Skill は orchestrator から static dispatch で
起動された worker の実行本文としてのみ使われる。

## Instruction Boundary（本手順が扱う指示衛生の境界線を明確化する）

本 Skill には以下を **一切含めない**（Issue #1733）:

- `post-merge-cleanup-worker` 自身の再起動指示
- follow-up Issue の起票実行（候補列挙のみ。実行は main thread / orchestrator）
- parent issue クローズの実行（条件確認のみ。実行は main thread / orchestrator）
- superseded PR の close / comment 実行（候補列挙のみ。実行は main thread / orchestrator）
- main session への最終 routing 指示

worker がこの手順本文だけを読む限り、role confusion（自分が main-thread orchestrator であるかのような指示の混入）が起きない。

## no-child policy（子エージェントへの入れ子委譲を完全に禁止する方針）

worker は次のいずれの経路でも子 agent を起動しない:

- `Agent` tool 経由のネスト委譲（`post-merge-cleanup-worker.md` の `disallowedTools: [Agent]` で継続的に禁止）
- Bash 経由の外部 agent CLI 起動（例: `codex exec`、`claude -p` 等）。本手順のどの bash コマンド例にも
  `codex exec` / `claude -p` の呼び出しを含めない。worker が Bash tool を保有していても、本手順は
  git / gh / 本リポジトリの `scripts/agent-ops/*.py` / `scripts/check_post_merge_cleanup_boundary.py`
  以外のコマンド起動を指示しない。

## Procedure（機械的実行手順・8 ステップ）

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
- `tmp/` は repo-approved local temporary workspace の canonical write destination であり、`.claude/tmp/` は非推奨（deprecated）の legacy root である（Issue #1995）。deprecated であっても`never_delete` / `report_only` の safety boundary は変更せず、構造・read/scan/report は継続する。
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
**defense-in-depth** として残す内部機構である。本 Skill は bare git cleanup を案内しない。

削除できないものは `unresolved_cleanup_items` に記録する。

**discard レーン routing（Issue #1523 fix_delta P1-2）**: `cleanup_exec` が `reason_code:
pr_head_oid_mismatch`（または local-only 未公開コミットの discard candidate シグナル）を返した場合、
worker は破壊的操作を一切実行せず、以下の non-destructive `--check` probe のみを実行する:

```bash
uv run --locked python3 scripts/agent-ops/materialize_cleanup_contract.py \
  --pr-number <pr> --linked-issue-number <issue> \
  --worktree-path <絶対 worktree path> --branch-name <branch> \
  --operation local_only_discard --check --json
```

`status: confirmation_required` の場合は、続けて `--check` なしで同一 argv を発行し（`--operation
local_only_discard`、`--check`/`--consume` いずれも付けない issuance 呼び出し）、target+SHA-bound
immutable per-nonce contract を materialize する。この issuance 呼び出しの応答 JSON に含まれる
`confirmation` block（`contract_id` / `contract_sha256` / `pr_head_sha` / `local_tip_sha` /
`local_only_commit_shas` / `expires_at` / `argv`）を **そのまま** `POST_MERGE_CLEANUP_REPORT_V1` の
`discard_confirmation` field へ転記し、レポートで人間に提示する。

worker はこの `confirmation.argv`（`--consume --contract-id <nonce> --expected-contract-sha256 <digest>`
を含む）を **自分では実行しない**。`--consume` は人間専用のコマンドであり
（`scripts/agent-guards/worktree_scope_guard.py` の argv allowlist が agent 発行の `--consume` を deny
する）、worker の責務は confirmation block を正確に報告することで完結する。

### 4. parent issue クローズ条件確認


`merged_pr_number` から linked issue → parent issue を辿り、parent の他 child の状態を確認:

```bash
gh api repos/{owner}/{repo}/issues/{linked_issue}/parent --jq '.number'
gh api repos/{owner}/{repo}/issues/{parent_issue}/sub_issues --jq '.[] | {number, state}'
```

全 child がクローズ済み → `parent_issue_status.recommended_action: close` を返す。**close 実行は main thread（orchestrator）**。本ステップは条件確認のみで close コマンドを発行しない。

### 5. Superseded PR 候補抽出

`merged_pr_number` 未提供時は skip して `unresolved_cleanup_items` に `merged_pr_number not provided, steps 5/6 skipped` を記録。

同じ Issue を Closes する他の OPEN PR を検索する。`gh pr list --search "linked:issue/<N> is:open"` の
`linked:issue/<N>` は GitHub が公式に文書化した search qualifier ではないため使用しない。代わりに
`gh issue view --json closedByPullRequestsReferences` （公式に文書化された GitHub CLI フィールド）を使う:

```bash
gh issue view "$linked_issue" \
  --json closedByPullRequestsReferences \
  --jq '.closedByPullRequestsReferences[] | select(.state == "OPEN") | {number,title,url}'
```

結果から現在の merged PR 自身（`merged_pr_number`）を除外し、残りの候補を `superseded_prs` に列挙して返す
（close / comment の実行は main thread（orchestrator））。

### 5a. （廃止・Issue #1873）

`pr_review.publish` コマンド ID および対応する controlled executor
（`scripts/agent-ops/pr_review_marker_archive_exec.py`）は Issue #1873 で撤去された。
このステップは無効化されている。過去に生成された
`artifacts/<pr>/issue-metadata/pr_review.publish/pr_review_publish.marker.json`
（`PR_REVIEW_PUBLISH_MARKER_V1`）が残存していても本 Skill は何も行わない。

### 6. Follow-up 候補の収集

merged PR の本文 / コメントから以下を抽出:
- `## Follow-ups Intentionally Deferred` セクション（あれば）
- レビューコメントで follow-up 化が示唆された項目

候補を `follow_up_issue_requests` に `FOLLOW_UP_ISSUE_REQUEST_V1` 形式で列挙する（起票実行は main thread（orchestrator）が `issue-author` SubAgent / `create-issue` 経由で実行。本ステップでは `gh issue create` を呼び出さない）。

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
| `create_issue` | `severity: optional_follow_up` の `FOLLOW_UP_ISSUE_REQUEST_V1` を生成（起票実行は main thread（orchestrator）） |
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

後述の Output 仕様で YAML を返す。生成した YAML は `scripts/check_post_merge_cleanup_boundary.py` の
`validate_report_v1` 相当のスキーマ（必須キー・型・不明キー拒否）を満たす必要がある。

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
  stash_restored: true | false | n/a
  stash_entry_ref: "<stash@{N} or null>"
  warnings: []
  errors: []
  discard_confirmation: null | { ... }  # optional (Issue #1523 fix_delta P1-2)
```

closed-key スキーマ: 上記トップレベルキー以外を含む YAML は不正とみなす（`scripts/check_post_merge_cleanup_boundary.py` の `validate_report_v1` を参照）。`discard_confirmation` は additive-only の optional field（Issue #1523 fix_delta P1-2）: discard candidate が見つからない通常の実行では `null`（または省略）。見つかった場合は `materialize_cleanup_contract.py` issuance 呼び出しが返した `confirmation` block をそのまま格納する:

```yaml
discard_confirmation:
  contract_id: "<nonce>"
  contract_sha256: "<sha256 hex>"
  pr_head_sha: "<sha>"
  local_tip_sha: "<sha>"
  local_only_commit_shas: []
  expires_at: "<ISO 8601>"
  argv: []  # 人間が実行する --consume 完全コマンド。worker はこれを実行しない
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
- follow-up 起票は本手順内で実行しない（候補列挙のみ。実行は main thread（orchestrator））
- parent issue close / superseded PR close は本手順内で実行しない（候補列挙のみ。実行は main thread（orchestrator））
- worktree / branch の削除は確定条件を満たすもののみ。曖昧なら `unresolved_cleanup_items` に記録
- **scripts entrypoint 経由統一**: git 状態の分類は必ず `.claude/skills/post-merge-cleanup/scripts/classify-git-state.py` 経由で実行する
- **inline `gh` / `jq` / `grep` / `awk` / heredoc 使用禁止**: ステップ 1 の git 状態分類での inline bash パイプラインは使用しない
- **スクリプトは `subprocess.run([...])` 配列形式のみ**: `shell=True` 禁止
- **root temporary residue の削除実行禁止**: `temp_residue_classification` の `recommendation: eligible_for_delete` を見ても、本 Skill / SubAgent は削除を実行しない（read-only classifier の出力を消費するのみ）
- **本手順のいかなるステップでも `post-merge-cleanup-worker` の再起動・main-thread routing（follow-up 起票実行・parent close 実行・superseded PR close 実行・main session への最終 routing）を実行しない**
- **Bash 経由の外部 agent CLI（`codex exec` / `claude -p` 等）起動を一切指示しない**

## Related / 関連

- `.claude/agents/post-merge-cleanup-worker.md` — 本 Skill を実行する SubAgent（`skills: [post-merge-cleanup-executor]`）
- `.claude/skills/post-merge-cleanup/SKILL.md` — top-level orchestrator（worker 起動・follow-up/parent/superseded routing）
- `.claude/skills/create-issue/SKILL.md` — follow-up 起票委譲先（実行は orchestrator）
- `scripts/agent-ops/cleanup_exec.py` — worktree / branch 削除の単一認可境界
- `scripts/check_post_merge_cleanup_boundary.py` — orchestrator/executor 責務境界と `POST_MERGE_CLEANUP_REPORT_V1` の validator
- `scripts/agent-ops/temp_residue_classifier.py` — root temporary residue の read-only classifier（Issue #1417）
- `scripts/agent-ops/temp_residue_marker.py` — `temp_residue_owner/v1` ownership marker parser（Issue #1417）
- `schemas/temp_residue_classification_v1.schema.json` / `schemas/temp_residue_owner_v1.schema.json`
- `docs/dev/repository-folder-policy.md` — folder class / cleanup authority の正本
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界

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
cleanup 経路ではない。本 Skill の cleanup は `cleanup_exec` 経由のみで発行し、
agent が bare 形式の `git worktree remove` / `git branch -d` を直接発行する手順は採用しない。

### worktree_path の制約

`worktree_path` は `<project_root>/.claude/worktrees/` の直下のパスである必要があります。
それ以外のパス（project root 自体、任意のファイルシステムパス等）は deny されます。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`POST_MERGE_CLEANUP_REPORT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
