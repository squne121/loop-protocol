---
name: implement-issue
description: 承認済みの implementation child issue（`issue-contract-review` で go 判定済み）を、Allowed Paths 内で実装し、Verification Commands で検証し、Draft PR を作成して Issue コメントに結果を返すまでを `1 Issue = 1 PR` で進める手順。「Issue ◯◯ 実装して」「implement issue」「この Issue やって」のトリガーで使う。
---

# Implement Issue

承認済み contract に従い、implementation child issue を実装し、verify、PR、Issue 更新まで進める手順。
live Issue contract を取得できれば呼び出せる。artifact は着手権限ではない。

## Input（入力）

- `Issue番号` または `Issue URL`（必須）
- `issue-contract-review` の contract-snapshot comment URL（任意 telemetry）

Note: `contract_snapshot_url` の欠落・stale・invalid は実装停止理由にしない。
live Issue、canonical linked worktree、Allowed Paths、実テストを正本とする。

## Procedure（手順）

### 1. Issue contract を再取得

```bash
ISSUE_NUMBER=<番号>
REPO=$(git remote get-url origin | sed 's/.*github.com[:/]//' | sed 's/\.git$//')
gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json title,body,labels,comments
```

確認項目（`issue-contract-review` で確認済みだが再確認）:
- `## Outcome`
- `## Acceptance Criteria`
- `## Verification Commands`
- `## Allowed Paths`
- `## Stop Conditions`
- 最新コメントに `## Contract Snapshot` があり、それと本文が整合している

consumer ready contract（title `実装:` または `implement:`、dependency all closed）が揃っているかを確認する。`phase/implementation` label は routing/classification 用の非authoritative な参照であり（presentation-only metadata、#2084）、readiness gate の必須条件ではない。legacy state label（`state/needs-human` 含む）の有無だけを理由に停止してはならない。最新 `CONTRACT_REVIEW_RESULT_V1 status` は任意の telemetry として記録するのみで、`go` 以外（missing/stale/invalid を含む）でも実装を停止しない（#1860 Owner Decision）。live Issue 本文・Allowed Paths・実テストが正本である。

peer OPEN Issue の overlap preflight（旧 Step 2 の候補収集レイヤー
production 呼び出し）は #1679 により production path から完全に撤去された
（#1860 Owner Decision: OPEN Issue 全件収集・semantic overlap 判定は通常実装の
停止権限を持たない新しい planning authority を作らない）。`implement-issue` は
target Issue、canonical repository、worktree、実 diff、実 test、target PR、
current-head CI、独立 review、human stop だけを実行判断入力とする
target-only executor である。peer OPEN Issue の body・comments・native
dependency は読み取らず、`gh issue list` / GraphQL による OPEN Issue 全件収集も
行わない。

overlap／collision 由来で扱える停止理由は、実際の Git conflict
（target PR の GitHub mergeability が `mergeable == CONFLICTING` または
`mergeStateStatus == DIRTY` の場合。`UNKNOWN` / `BLOCKED` / `BEHIND` /
`UNSTABLE` は競合として扱わない）のみに限定される。これは overlap
preflight 撤去に伴う限定であり、他の既存 hard gate を無効化するものでは
ない。Allowed Paths、canonical repository resolution／mutation target
binding、CI・required checks・branch protection、独立 review
（`pr-review-judge`）、human stop（Issue/PR コメント上の明示的な停止指示）、
Step 4（Verification Commands）の失敗、root checkout／detached HEAD／dirty
worktree、protected paths、secret、destructive Git operation、publish
approval、test・lint・typecheck・build は、overlap 撤去とは独立した
既存 hard gate として維持され、いずれも実装停止理由になり得る。

### 2. Worktree / Branch 作成手順

```bash
SLUG="<short-slug>"  # contract-snapshot の Worktree フィールドから取得
WORKTREE=".claude/worktrees/issue-${ISSUE_NUMBER}-${SLUG}"
BRANCH="worktree-issue-${ISSUE_NUMBER}-${SLUG}"

# 1. executor を実行して BOOTSTRAP_JSON を取得
BOOTSTRAP_JSON=$(uv run --locked python3 scripts/agent-ops/worktree_bootstrap_exec.py \
  --issue-number "$ISSUE_NUMBER" \
  --slug "$SLUG" \
  --branch-name "$BRANCH" \
  --worktree-path "$WORKTREE" \
  --base-ref main \
  --json)

# 2. status が ok_created または ok_existing であることを確認
BOOTSTRAP_STATUS=$(uv run python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" <<< "$BOOTSTRAP_JSON")
if [ "$BOOTSTRAP_STATUS" != "ok_created" ] && [ "$BOOTSTRAP_STATUS" != "ok_existing" ]; then
  echo "ERROR: worktree executor returned status=$BOOTSTRAP_STATUS" >&2
  echo "$BOOTSTRAP_JSON" >&2
  exit 1
fi

# 3. worktree_path を取得
WORKTREE=$(uv run python3 -c "import json,sys; print(json.load(sys.stdin)['worktree_path'])" <<< "$BOOTSTRAP_JSON")

# 4. cd "$WORKTREE" して worktree に移行
cd "$WORKTREE"

# 5. git branch --show-current が "$BRANCH" と一致することを検証
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
  echo "ERROR: branch mismatch: expected=$BRANCH actual=$CURRENT_BRANCH" >&2
  exit 1
fi
```

executor が `status: ok_created` または `status: ok_existing` を返したら worktree の準備完了。`status: blocked` または `status: failed` の場合は人間判断を仰ぐ。`WORKTREE_BOOTSTRAP_RESULT_V1.worktree_path` が `IMPLEMENT_RESULT_V1.worktree` にマップされる（`branch` フィールドは `IMPLEMENT_RESULT_V1.branch` にそのままマップ）。

- **配置先は必ず `.claude/worktrees/` 配下**（リポジトリ外配置禁止 — workspace trust prompt 再発防止）
- 既存衝突は `issue-contract-review` で検出済みのため、ここで衝突した場合は人間判断を仰ぐ

worktree 内で Edit / Write する際は **必ず worktree 内の絶対パス**を指定する。main の絶対パスを指定すると main のファイルが変更される事故が起きる。

### 2.5. Runtime Verification Applicability（動作検証適用範囲）の確認

Issue 本文の `## Runtime Verification Applicability` を確認する。

- `decision: not_applicable` → runtime AC / VC / 証跡は不要。静的検証のみで実装を完結させる。
- `decision: immediate` → 動作検証 AC に対応する VC スクリプト（bash / pytest 等）と `artifacts/` 出力ロジックを実装する。証跡を PR 本文に添付する。実行環境が不可なら SKIP exit 77 を返す（SKIP = PASS ではない）。`deferred` の動作検証を捏造しない。
- `decision: deferred` → 後続 Issue / 統合フェーズ / 検証条件を PR 本文に引用するのみ。`deferred` の動作検証を本 Issue の実装中に捏造しない。証跡の提出は後続 Issue / フェーズで行う。
- live Issue body の top-level canonical `## Runtime Verification Applicability` セクション自体が存在しない場合は、`issue-contract-review` の結果、contract snapshot、旧コメントを代替根拠にせず **実装を開始せず人間にエスカレーション**する（`issue-refinement-loop` 経由で Issue 本文を更新させるか、呼び出し元に `status: blocked` を返す）。`not_applicable` と推定して実装を進めてはならない。

詳細は `docs/dev/runtime-verification-policy.md` の「Runtime Verification Applicability」を参照する。

### 3. TDD + BDD で実装

LOOP_PROTOCOL のテスト戦略に従う:

- **TDD**: 実装前に Vitest テストを書く（`tests/<対象>.test.ts`）
- **BDD**: テスト名は GIVEN/WHEN/THEN 形式
- 各 AC に対応するテストを少なくとも 1 つ書く
- 境界値（0、最大値、空入力）と異常系を含める

実装中の制約:
- **Allowed Paths 外を編集しない**（CLAUDE.md / per-directory CLAUDE.md の制約も遵守）
- スコープ外の改善・リファクタリングを混ぜない（別 Issue で扱う）
- `git add -A` / `git add .` を使わず、変更ファイルを明示してステージング

### 4. Verification Commands を実行

Issue 本文の `## Verification Commands` を順に実行する。

LOOP_PROTOCOL の標準 4 コマンドが含まれている前提:
```bash
pnpm typecheck   # TypeScript 型エラーなし
pnpm lint        # ESLint エラーなし
pnpm test        # Vitest 全件 PASS
pnpm build       # vite build 成功
```

途中で fail したら **自己修正してから次へ進む**。修正が困難な場合は人間判断を仰ぐ。

各コマンドの結果（PASS / FAIL + 関連出力）を後段の PR 本文の「検証コマンド結果」セクションに残すため記録する。

### 5. コミット

```bash
# 変更ファイルを明示してステージング（git add -A 禁止）
git add <path1> <path2> ...

git commit -m "$(cat <<'EOF'
<type>: <subject> (#<issue>)

<body — なぜこの変更が必要か、影響範囲>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- Conventional Commits 風: `feat` / `fix` / `refactor` / `docs` / `chore` / `test`
- `--no-verify` 禁止（Git Hooks をすり抜けない）
- WIP コミットを push しない（push 前に rebase / squash で整理）

### 6. push & PR 起票（`open-pr` skill に委譲）

PR 起票は本 skill の責務外。`open-pr` skill に委譲する。

```bash
git push -u origin "$BRANCH"
```

push 完了後、以下を `open-pr` skill に渡して起票させる:

- `linked_issue`: `$ISSUE_NUMBER`
- `pr_title`: `<type>: <subject>`
- `contract_snapshot_url`: 受け取った contract-snapshot comment URL
- `verification_summary`: ステップ 4 で記録した PASS / FAIL サマリ
- `allowed_paths_compliance`: true / false

PR 本文テンプレ・publish ゲート・idempotency チェック・`Closes`/`Refs` の使い分けは `open-pr` 側の責務。本 skill では `gh pr create` を直接呼ばない。peer OPEN Issue の overlap preflight は production path から撤去済みであり（#1679）、`open-pr` へ渡す入力に overlap evidence は含まない。

### 7. Issue コメントへの結果報告

```bash
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body "## implement-issue: 実装完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- PR: <PR URL>
- Worktree: \`$WORKTREE\`
- Branch: \`$BRANCH\`
- Verification: 4/4 PASS
- 後続: PR レビュー（pr-review-judge）→ マージ → post-merge-cleanup"
```

## Output（出力結果） (IMPLEMENT_RESULT_V1)

```yaml
IMPLEMENT_RESULT_V1:
  status: ok | failed | blocked
  generated_at: <ISO 8601>
  generated_by: implement-issue
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  pr_url: https://github.com/<owner>/<repo>/pull/<番号>
  worktree: .claude/worktrees/issue-<番号>-<slug>
  branch: worktree-issue-<番号>-<slug>
  verification:
    typecheck: pass | fail
    lint: pass | fail
    test:
      passed: <count>
      failed: <count>
      files: <count>
    build: pass | fail
  allowed_paths_compliance: true | false
  warnings: []
  errors: []
```

## Conflict Resolve（pr-review-judge から差し戻された場合）

`pr-review-judge` SubAgent から `LOOP_VERDICT: REQUEST_CHANGES + blockers: [merge_conflict]` を受け取った場合、`impl-review-loop` の CONFLICTING PR Escalation Runbook（C-4 で整備予定）に従って resolve する。

## Guardrails（ガードレール）

- **Allowed Paths 外を編集しない**（ルート `CLAUDE.md` + per-directory `CLAUDE.md` の保護領域も遵守）
- `assets/` / `LICENSES/` は AI 編集禁止（明示指示があっても skill 内では拒否）
- スコープ肥大化を防ぐ（別の問題は別 Issue 化）
- `git add -A` / `git add .` 禁止（意図しないファイル混入防止）
- `--no-verify` 禁止（Git Hooks をすり抜けない）
- WIP コミットを push しない
- `1 Issue = 1 PR` を厳守
- worktree はリポジトリ内 `.claude/worktrees/` 配下（外部配置禁止）
- `## Required Skills` に `issue-contract-review` / `implement-issue` / `pr-review-judge` 等のワークフロースキルが列挙されていても「preload されていないため開始できない」とは判断しない（暗黙的に適用されるため）

## Verification Commands 失敗時の対処

- **環境構築の副作用**（依存パッケージ初回インストール等）で初回 exit 1 になる場合、2 回目を実行する。Commands Run には「初回 exit 1（環境構築）、2 回目 exit 0」と明記する
- 環境依存で実行不能な場合は、個別コマンドに分解して実行し、その旨を Commands Run に記録する

## Related（関連情報）

- `.claude/skills/issue-contract-review/SKILL.md` — 着手前 preflight（本 skill の前段）
- `.claude/skills/impl-review-loop/SKILL.md` — 実装→検証→PR レビュー の 4 段ループ（オーケストレーター）
- `.claude/skills/open-pr/SKILL.md` — PR 起票手順（C-4 で整備予定）
- `.claude/skills/post-merge-cleanup/SKILL.md` — PR マージ後の cleanup
- `.claude/skills/ssot-discovery/SKILL.md` — 実装着手前の SSOT 探索
- `.claude/agents/implementation-worker.md` — 本 skill を使う SubAgent
- `.claude/agents/test-runner.md` — Verification Commands を実行する SubAgent
- ルート `CLAUDE.md` + per-directory `CLAUDE.md` — 不変条件の正本
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`IMPLEMENT_RESULT_V1` の全フィールドは必ず含める（routing 必須フィールド）。

## update_branch Contract（ブランチ更新契約）

`impl-review-loop` Step 5 の BEHIND 分岐から呼び出される `update_branch` 実行手順の contract。

### UPDATE_BRANCH_REQUEST_V1

```yaml
UPDATE_BRANCH_REQUEST_V1:
  pr_number: <int>           # 対象 PR 番号
  repo: <owner/repo>         # 例: squne121/loop-protocol
  expected_head_sha: <sha>   # Step 4 の reviewed_head_sha（race guard 用）
  update_method: merge_only  # 固定値。GraphQL/rebase は out-of-scope（follow-up issue で対応）
  caller: <string>           # 呼び出し元識別子（例: impl-review-loop.step-5）
```

`update_method: merge_only` は REST `PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch` の merge update 固定を表す。GraphQL `updatePullRequestBranch` mutation および rebase update は本 contract の Out of Scope — 別 Issue で対応する。

### UPDATE_BRANCH_RESULT_V1

```yaml
UPDATE_BRANCH_RESULT_V1:
  status: ok | failed | blocked | permission_blocked
  reason_code: null | expected_head_sha_missing | expected_head_sha_mismatch | permission_denied | primary_rate_limit | secondary_rate_limit | validation_failed | head_unchanged_after_accepted | unexpected_head_change | transport_error | unknown_http_status
  update_method: merge_only  # リクエストの update_method を echo（検証用）
  http_status: 202 | 403 | 422 | 429 | <other>
  before_head_sha: <sha>
  before_base_sha: <sha | null>  # 202 受理直後に取得した base ブランチ head（postcondition の祖先関係検証用、#1429 iteration-1）
  after_head_sha: <sha>
  new_head_sha: <sha>    # 202 + poll 成功時のみ（head 更新後の headRefOid）
  poll_attempts: <int>
  rerun_required:
    verification: true | false
    pr_review: true | false
    reason: <string | null>
  permission_diagnostics:  # 403 permission denied 時のみ
    auth_actor: <string>
    head_repo: <owner/repo>
    base_repo: <owner/repo>
    fork_pr: true | false
    maintainer_can_modify: true | false
    required_permissions: <string>
  rate_limit_diagnostics:  # primary_rate_limit / secondary_rate_limit 時のみ
    retry_after_seconds: <int | null>
    x_ratelimit_remaining: <int | null>
    x_ratelimit_reset: <epoch | null>
  error_body: <string>   # 分類根拠の body
  errors: []
```

`reason_code: unexpected_head_change` は、202 Accepted 後の poll で headRefOid が変化したものの、`expected_head_sha` および `before_base_sha` の祖先関係（GitHub compare API 経由）を検証できなかった場合に返す（malformed SHA、無関係な commit、concurrent force-push 等）。この場合 `status: blocked` とし `rerun_required.verification` / `rerun_required.pr_review` をいずれも `true` にする（#1429 iteration-1 P1-2 — 従来は headRefOid が単に `expected_head_sha` と異なるという理由のみで `status: ok` を返しており、この postcondition 検証が欠落していた）。

`primary_rate_limit` は `x-ratelimit-remaining: 0` ヘッダで判定する一次 REST レート制限、`secondary_rate_limit` は abuse-detection / secondary rate limit メッセージ本文で判定する二次レート制限であり、両者は互いに独立した reason_code として区別する（#1429 iteration-1 P2 — 従来は本文中の汎用 `"rate limit"` 文字列一致のみで一次レート制限応答も `secondary_rate_limit` に誤分類しうる状態だった）。

### 呼び出し形式

production caller（`implementation-worker` を含む）は、raw `gh api` を直接実行せず、次の canonical `update_branch.py` invocation のみを使用する。

```bash
uv run --locked python3 \
  .claude/skills/implement-issue/scripts/update_branch.py \
  --pr-number <pull-request-number> \
  --repo squne121/loop-protocol \
  --expected-head-sha <reviewed-head-sha> \
  --caller impl-review-loop.step-5 \
  --update-method merge_only
```

`update_branch.py` は `UPDATE_BRANCH_REQUEST_V1` の各フィールドを上記 CLI 引数へそのままマッピングし、標準出力へ `UPDATE_BRANCH_RESULT_V1` 相当の JSON を出力する（exit code は `status == ok` で 0、それ以外で 1）。

`update_branch.py` は API 呼び出し前に次を自己検証し、違反時は GitHub API を呼ばず `reason_code: validation_failed`（またはその他の対応する reason_code）を返す:

- `pr_number > 0`
- `repo == squne121/loop-protocol`（canonical repository binding）
- `expected_head_sha` が完全長 hexadecimal commit SHA（非空の場合）
- `caller` が既知の caller ラベル一覧（`KNOWN_CALLER_LABELS`。`impl-review-loop.step-5` 等）に含まれる
- `update_method == merge_only`

`caller` チェックは既知ラベルの typo 検知に過ぎず、呼び出し元プロセス／identity を独立検証する authorization・provenance 機構ではない（#1429 iteration-1 P2）。

GitHub の branch 更新 REST エンドポイントへの raw `gh api` 直接呼び出し、および `gh` の branch 更新用サブコマンドは、`update_branch.py` の wrapper 内部実装としてのみ使用され、production caller が直接実行することはない。

本 contract は **REST merge update 固定**（`update_method: merge_only`）。linear history またはリベース必須リポジトリは out-of-scope であり、403 / 422 / 429 / transport error は `UPDATE_BRANCH_RESULT_V1` の reason_code に決定論的に正規化する。GraphQL `updatePullRequestBranch` mutation および rebase update は本 contract 対象外（Out of Scope）。

### HTTP ステータス別分岐（wrapper 内部実装の責務）

以下は `update_branch.py` 内部が行う分岐であり、production caller が個別に bash で再実装するものではない。

**202 Accepted（リクエスト受理）:**

update が受け付けられた。headRefOid が `EXPECTED_HEAD_SHA` から変化するまで poll する（bounded retry: 5 秒 × 最大 12 回。この既定値は production invocation では caller から緩和できない固定値 — `PRODUCTION_POLL_MAX` / `PRODUCTION_POLL_INTERVAL`）。

- poll 中に headRefOid が変化し、かつ `expected_head_sha` と `before_base_sha`（poll 開始直前に取得した base ブランチ head）の両方が新 headRefOid の祖先であることを GitHub compare API で検証できた場合 → `UPDATE_BRANCH_RESULT_V1.status: ok`、`new_head_sha: <新 headRefOid>` を記録
- poll 中に headRefOid が変化したが、上記祖先関係を検証できなかった場合（malformed SHA、無関係な commit、concurrent force-push 等）→ `status: blocked` / `reason_code: unexpected_head_change`（fail-closed。「headRefOid が単に変化した」だけでは `ok` としない）
- bounded retry 上限到達まで変化なし → `status: failed` / `reason_code: head_unchanged_after_accepted`

**403 Forbidden（権限拒否）:**

権限不足またはフォーク PR の書き込み制限。`permission_diagnostics`（auth_actor、head_repo、base_repo、fork_pr、maintainer_can_modify、required_permissions）を出力して `status: permission_blocked` / `reason_code: permission_denied` とする。`required_permissions` には `pull_requests:write` と `contents:write_on_head_repository_when_github_app` の両方を明記する。

**422 Unprocessable Entity（処理不可）:**

body 内容で分類する（422 全体を `expected_head_sha` mismatch とは断定しない）:

| body の内容 | status |
|---|---|
| `expected_head_sha` mismatch | `expected_head_sha_mismatch` — Step 4 re-review 後 Step 5 再実行 |
| abuse-detection / secondary rate limit メッセージ | `secondary_rate_limit` — fail-closed。header 由来の diagnostics を返して再実行判断を人間へ委譲 |
| その他 validation failure | `validation_failed` |

**403 / 429（一次・二次レート制限の区別）:**

403・429 応答は `x-ratelimit-remaining: 0` ヘッダを一次レート制限（`reason_code: primary_rate_limit`）の判定に使う。abuse-detection / secondary rate limit のメッセージ本文一致は二次レート制限（`reason_code: secondary_rate_limit`）として別に分類する。汎用的な `"rate limit"` 文字列一致のみでの判定は行わない（#1429 iteration-1 P2 — 一次レート制限応答の誤分類を避けるため）。

### Bash 許可例外

`implementation-worker`（`.claude/agents/implementation-worker.md`）は `update_branch.py` の canonical invocation（上記「呼び出し形式」）のみを Bash 操作例外として許可される。GitHub の branch 更新 REST エンドポイントへの raw 直接呼び出しを production caller として実行することは許可されない。

## IMPLEMENTATION_WORKER_REQUEST_V2 対応

`impl-review-loop` から `IMPLEMENTATION_WORKER_REQUEST_V2` を受け取った場合、worker は PR repair executor として動作する。詳細スキーマ・routing table・各 mode の挙動は `.claude/agents/implementation-worker.md` の `IMPLEMENTATION_WORKER_REQUEST_V2` セクションを参照すること。

### update_pr_body_hygiene（PR body 更新）

`update_pr_body_hygiene` mode では `open-pr/scripts/update_pr.py` wrapper 経由での実行を必須とする。
**`gh pr edit --body-file` の直接呼び出しは本 SKILL.md からも禁止**（wrapper 内部実装としての使用は例外）。

### update_branch（PR ブランチ更新）

`update_branch` mode は本 SKILL.md の `UPDATE_BRANCH_REQUEST_V1` contract を使用する（上記セクション参照）。
`IMPLEMENTATION_WORKER_REQUEST_V2.expected_head_sha` が未指定の場合は `UPDATE_BRANCH_REQUEST_V1` を発行せず `status: blocked` を返す。
