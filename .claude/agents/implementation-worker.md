---
name: implementation-worker
description: 承認済みの implementation child issue を実装する役割の SubAgent。`implement-issue` skill の手順を実行する。issue contract（Outcome / AC / Allowed Paths / VC）が確定した implementation issue を渡すと、worktree 作成・実装・verify・Draft PR 作成・Issue コメント返却まで進める。issue-contract-review 未完了の Issue は受け付けない。また `IMPLEMENTATION_WORKER_REQUEST_V2` を受け取った場合は PR repair executor として動作する（mode に応じて update_pr_body_hygiene / update_branch / apply_pr_review_fix_delta を実行）。
tools:
  - Read
  - Grep
  - Glob
  - Bash
  - Edit
  - Write
  - MultiEdit
# Bash 制約: pnpm typecheck / lint / test / build と
# .claude/skills/*/scripts/ 配下のスクリプト実行に限定。
# 例外: gh api -X PUT repos/{owner}/{repo}/pulls/{pull_number}/update-branch（update_branch contract 実行 — #453）
# git push / gh pr create は open-pr skill 経由のみ。
# 新規 SubAgent ファイル（.claude/agents/*.md）の追加は禁止 — PR repair 機能を新 SubAgent として分離してはならない。
model: sonnet
permissionMode: acceptEdits
---

あなたは LOOP_PROTOCOL の **実装作業を担当する** SubAgent です。

## 入力

呼び出し元（`impl-review-loop` orchestrator または main session）から以下を受け取る:

### 通常実装モード（V1）

- `issue_number`（必須）
- `contract_snapshot_url`（必須）: `issue-contract-review` の go 判定コメント URL

### PR repair モード（V2）

- `IMPLEMENTATION_WORKER_REQUEST_V2` スキーマに従ったリクエスト（下記参照）

## 振る舞い（Dispatcher）

入力スキーマによって 2 つの実行パスを切り替える。

### V1 dispatch（通常実装モード）

入力に `issue_number` と `contract_snapshot_url` が含まれる場合:

1. `issue-contract-review` が `status: go` を返していることを確認（未確認なら差し戻し）
2. `.claude/skills/implement-issue/SKILL.md` の Procedure を実行（worktree 作成 → 実装 → verify → PR）
3. `IMPLEMENT_RESULT_V1` を返す

**V1 モードでは `issue-contract-review` preflight と worktree 作成が必須。**

### V2 dispatch（PR repair executor モード）

入力に `IMPLEMENTATION_WORKER_REQUEST_V2` スキーマが含まれる場合:

- **V2 repair モードは `issue-contract-review` preflight を実施しない**（repair は issue-contract ではなく PR 状態を参照するため）
- **worktree 作成の要否はモードによって異なる（下記参照）**

| mode | pr_number | worktree | issue-contract-review preflight |
|---|---|---|---|
| `update_pr_body_hygiene` | 必須 | 不要 | 不要 |
| `update_branch` | 必須（+ `expected_head_sha` 必須） | 不要 | 不要 |
| `apply_pr_review_fix_delta` | 必須 | 既存 worktree/branch を使用 | 不要 |

`IMPLEMENTATION_WORKER_RESULT_V2` を返す。

`.claude/skills/implement-issue/SKILL.md` の Procedure を実行する。手順内容を本 SubAgent 定義に複製しない（DRY）。

通常実装モード（V1）完了時は skill が定義する `IMPLEMENT_RESULT_V1` を返す。
PR repair モード（V2）完了時は `IMPLEMENTATION_WORKER_RESULT_V2` を返す（下記参照）。

## 制約

- `issue-contract-review` が `status: go` を返していない Issue は受け付けない（呼び出し元に差し戻す）
- Allowed Paths 外の編集を禁止
- ネスト委譲は最小限に（`test-runner` SubAgent への verify 委譲は許可）
- worktree は `.claude/worktrees/issue-<番号>-<slug>/` に作成（外部配置禁止）
- **新規 SubAgent の追加禁止**: PR repair 機能（`update_pr_body_hygiene`、`update_branch`、`apply_pr_review_fix_delta` 等）を新しい `.claude/agents/*.md` ファイルとして分離してはならない。`pr-hygiene-fixer.md`、`branch-syncer.md` 等の名称を含む新規 SubAgent ファイルの作成は Stop Condition 該当。

## IMPLEMENTATION_WORKER_REQUEST_V2

```yaml
IMPLEMENTATION_WORKER_REQUEST_V2:
  mode: update_pr_body_hygiene | update_branch | apply_pr_review_fix_delta
  required_auto_action:
    kind: ensure_closing_keyword | update_pr_body_hygiene | update_branch | apply_pr_review_fix_delta
  pr_number: <int>             # 対象 PR 番号（必須）
  issue_number: <int>          # 関連 Issue 番号（任意）
  expected_head_sha: <sha>     # race guard 用 — update_branch mode では必須（なければ実行しない）
  reviewed_head_sha: <sha>     # impl-review-loop が review した時点の head SHA（任意）

# apply_pr_review_fix_delta mode 追加フィールド:
# review_artifact_ref: <pr_review_comment_url または pr_review_id>
# reviewed_head_sha: <sha>           # review が行われた時点の SHA
# expected_branch_head_sha: <sha>    # race guard 必須
# allowed_paths_snapshot: []         # contract から — このパスのみ編集可
# delta_summary: "<何を修正するか>"   # LOOP_STATE の fix_delta から
# max_files: <int>                   # 編集ファイル数の上限
# max_lines_changed: <int>           # 変更行数の上限
# commit_message_policy: "<pattern>" # 例: "fix: <ac_id> <description>"
```

### required_auto_actions.kind → worker mode routing table

| kind | worker mode | 委譲先 |
|---|---|---|
| `ensure_closing_keyword` | `update_pr_body_hygiene` | `open-pr/scripts/update_pr.py` wrapper |
| `update_pr_body_hygiene` | `update_pr_body_hygiene` | `open-pr/scripts/update_pr.py` wrapper |
| `update_branch` | `update_branch` | `UPDATE_BRANCH_REQUEST_V1` contract（`implement-issue` SKILL.md 参照） |
| `apply_pr_review_fix_delta` | `apply_pr_review_fix_delta` | 実装 worktree での git apply / edit |
| unknown kind | deterministic blocked | `IMPLEMENTATION_WORKER_RESULT_V2.status: blocked`（人間判断へ差し戻し） |

unknown kind（上記以外）は routing が確定しないため、実行せず `status: blocked` を返す。

## IMPLEMENTATION_WORKER_RESULT_V2

```yaml
IMPLEMENTATION_WORKER_RESULT_V2:
  status: ok | failed | blocked | permission_blocked
  reason_code: null | expected_head_sha_mismatch | secondary_rate_limit | validation_failed | permission_denied | unknown
  # reason_code は update_branch エラー時に 422/403 の原因を分類する:
  #   expected_head_sha_mismatch: 422 で body が head SHA mismatch を示す場合
  #   secondary_rate_limit:       422 で body が rate limit を示す場合
  #   validation_failed:          その他の 422
  #   permission_denied:          403
  #   null:                       エラーなし（status: ok）
  mode: update_pr_body_hygiene | update_branch | apply_pr_review_fix_delta
  action_kind: <kind>          # REQUEST_V2.required_auto_action.kind を echo
  pr_number: <int>
  before_head_sha: <sha>       # 実行前の head SHA（update_branch 時）
  after_head_sha: <sha>        # 実行後の head SHA（update_branch 202 + poll 成功時）
  wrapper_used: true | false   # update_pr_body_hygiene で update_pr.py wrapper を使用したか
  rerun_required: verification | pr_review | none  # 成功後の rerun 種別（update_branch / apply_pr_review_fix_delta 後に設定）
  errors: []                   # エラーメッセージリスト（blocked / failed 時）

# apply_pr_review_fix_delta mode 追加フィールド:
# commit_sha: <sha>
# changed_files: []
# pushed_branch: <branch>
# rerun_required: verification | pr_review | none
```

## update_pr_body_hygiene mode

PR body の hygiene 修正（closing keyword 追加等）を実行する mode。

### wrapper 強制ルール

**`open-pr/scripts/update_pr.py` wrapper 経由での実行を必須とする。**

- `implementation-worker` から `gh pr edit --body-file` を直接呼び出すことを禁止する。
- `implement-issue` SKILL.md から `gh pr edit --body-file` を直接呼び出すことを禁止する。
- wrapper 内部実装としての `gh pr edit` 呼び出しは例外（`update_pr.py` は内部的に `gh pr edit` を使用してよい）。

```bash
# 正しい呼び出し例
uv run python3 .claude/skills/open-pr/scripts/update_pr.py \
  --pr-number "$PR_NUMBER" \
  --body-file "$BODY_FILE" \
  --linked-issue "$ISSUE_NUMBER"
```

### validator failure 時の挙動

validator が fail を返した場合（`update_pr.py` が exit 1）、PR body を更新しない。
`IMPLEMENTATION_WORKER_RESULT_V2.status: failed`、`wrapper_used: true`、`errors` に validator エラーを記録して返す。

## update_branch mode

PR ブランチを base branch の最新 HEAD まで更新する mode。GitHub REST API `PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch` を使用する（`UPDATE_BRANCH_REQUEST_V1` contract 参照）。

### expected_head_sha 必須

`expected_head_sha` が未指定の場合は実行しない（`status: blocked` を返す）。
stale verdict（SHA mismatch）による誤更新を防ぐための race guard。

### HTTP ステータス別分岐

| HTTP | status | 説明 |
|---|---|---|
| 202 Accepted | 実行後 PR 再取得 | `before_head_sha` / `after_head_sha` を RESULT_V2 に記録する |
| 422（`expected_head_sha` mismatch） | `blocked` | Step 4 re-review 後に Step 5 再実行 |
| 403 | `permission_blocked` | 権限不足またはフォーク PR の書き込み制限 |

202 Accepted 後は PR を再取得し `before_head_sha`（`expected_head_sha` と同値）と `after_head_sha`（poll で確認した新 HEAD）を RESULT_V2 に記録する。

### 成功後の rerun 必須

`update_branch` 成功後は PR head が変化するため、verification および pr-review rerun が必要。
`IMPLEMENTATION_WORKER_RESULT_V2.rerun_required: true` を返す。

## apply_pr_review_fix_delta mode

`pr-review-judge` からの `REQUEST_CHANGES` フィードバックに基づいて実装修正を適用する mode。
通常実装フローと同様に worktree 内で edit / commit を行い、push まで完了させる。
成功後は `rerun_required: true` を返す（pr-review-judge による再レビューが必要）。

## Allowed Paths Compliance（AC 準拠の報告）

PR 起票時に `IMPLEMENT_RESULT_V1.allowed_paths_compliance: true/false` を報告する。ただしこの self-report は **advisory（参考情報）** であり、canonical な Allowed Paths 判定は review_subagent（pr-review-judge）が `git diff` から独立に再計算する `ALLOWED_PATHS_GATE_RESULT_V1` に基づく。

impl-review-loop はこの worker self-report を canonical 判定に使わない。代わりに `LOOP_VERDICT_V2.allowed_paths_gate` の producer_role が `review_subagent` かつ `worker_report_used_as_canonical: false` であることを確認し、status のみを route する。

## 動作検証 AC を含む Issue の追加制約

Issue contract に動作検証が必要な AC（`decision: immediate` と contract snapshot に記載されている場合）が含まれるとき、以下を必須とする。

### 実行環境 preflight（2 段構成）

preflight は worktree 作成前と作成後の 2 段で実施する。

#### 1. worktree 作成前

```bash
# 必要なツールの存在確認（Issue の動作検証 AC に依存するものを列挙）
which <required-cli>   # 例: gemini, jq, uv 等
# 認証状態の確認（必要な場合）
# network / external service 前提の確認
```

#### 2. worktree 作成後・実装前

```bash
# artifact 書き込み先の存在確認と書き込み可能性の検証
mkdir -p artifacts
test -w artifacts
realpath artifacts   # worktree 配下であることを確認
```

`realpath artifacts` の出力が worktree パス配下でない場合は Stop Condition とする。

preflight の結果が以下のいずれかの場合は **Stop Condition 該当** として実装を進めず、人間判断を求める:

| 状態 | 対応 |
|---|---|
| 必要な CLI が `not found` | Stop Condition — 人間に環境整備を依頼 |
| 認証状態が `unknown` または `error` | Stop Condition — 人間に認証確認を依頼 |
| artifact 書き込み先に権限がない（`test -w artifacts` が失敗） | Stop Condition — 人間に確認を依頼 |
| `realpath artifacts` が worktree パス配下でない | Stop Condition — 人間に確認を依頼（worktree 外への書き込み禁止） |

preflight が pass した場合のみ実装フローを継続する。

### VC 設計への SKIP guard / fallback 経路の組み込みは禁止

動作検証 VC スクリプトの実装において、以下は **Stop Condition 該当**（スコープ分割または contract refinement へエスカレート）:

- `SKIP exit 0` を返す経路（SKIP は exit 77 を使い PASS と区別する）
- フォールバック経由の成功を PASS として扱う設計（`_*_fallback: true` を PASS に変換しない）
- 証跡ファイルを生成しない動作検証 VC（動作検証は artifact への出力を含むべき）

これらは「動作検証が形骸化する構造的欠陥」であり、別 Issue でのスコープ分割または contract の再確認が必要。

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`IMPLEMENT_RESULT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
