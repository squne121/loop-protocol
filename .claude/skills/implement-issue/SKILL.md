---
name: implement-issue
description: 承認済みの implementation child issue（`issue-contract-review` で go 判定済み）を、Allowed Paths 内で実装し、Verification Commands で検証し、Draft PR を作成して Issue コメントに結果を返すまでを `1 Issue = 1 PR` で進める手順。「Issue ◯◯ 実装して」「implement issue」「この Issue やって」のトリガーで使う。
---

# Implement Issue

承認済み contract に従い、implementation child issue を実装し、verify、PR、Issue 更新まで進める手順。
`issue-contract-review` で `status: go` を得た後に呼ぶ。

## Input（入力）

- `Issue番号` または `Issue URL`（必須）
- `issue-contract-review` の contract-snapshot comment URL（必須）

Note: `contract_snapshot_url` の省略時自動検出・自動 materialize は #149 の責務。本 skill 単体では URL 入力を必須とし、URL 未提供時は `impl-review-loop` / #149 実装後の preparation に委譲する。

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

consumer ready contract（title `実装:` または `implement:`、routing label `phase/implementation`、`state/needs-human` 不在、dependency all closed、最新 `CONTRACT_REVIEW_RESULT_V1 status: go`）が揃っているかも確認する。legacy state label の有無だけを理由に停止してはならない。不一致なら停止して人間判断を仰ぐ。

### 2. Contract-aware overlap preflight（重複プリフライト、`check_implementation_overlap.py`）

本 Issue の Allowed Paths が他の OPEN implementation Issue と literal 一致するだけでは、実装開始を停止しない（#1452）。Allowed Paths の一致は「マージコンフリクトの可能性」を示すに過ぎず、Outcome / In Scope が意味的に disjoint な candidate（C1/C2a）は証跡を残した上で実装を継続できる。一方、意味的に重複する candidate（C2b/C3/duplicate）や、readback が不完全な candidate は fail-closed で人間判断へ停止する。

`.claude/skills/create-issue/scripts/check_issue_overlap.py` の pure classifier（`classify_overlap` / `IssueScope` / `SourceStatus` / path normalization）を正本として再利用し、implementation 専用の候補収集レイヤー `.claude/skills/implement-issue/scripts/check_implementation_overlap.py` を実行する:

```bash
uv run --locked python3 .claude/skills/implement-issue/scripts/check_implementation_overlap.py \
  --issue-number "$ISSUE_NUMBER" \
  --repo "$REPO" \
  --limit 2000 \
  > /tmp/overlap_preflight_${ISSUE_NUMBER}.json
```

`--limit` は GraphQL cursor pagination の収集総数に対する **safety cap**
であり（#1493）、ページサイズや取得件数の目標値ではない。全件性の証明は
GitHub GraphQL の `pageInfo.hasNextPage` が `false` になった時点で確定する
（`hasNextPage=false` に到達すれば、取得件数が page size（100）とちょうど
一致していても `source.complete: true` / `source.saturated: false` になる
— 固定件数への到達だけを理由に stop しない）。CLI 既定値は後方互換のため
`100`（safety cap としては小さすぎる）のままなので、本手順では明示的に
大きい値（`--limit 2000`）を指定する。呼び出し元が safety cap 自体を
超える巨大な候補集合に遭遇した場合（`source.saturated: true` /
`source.complete: false`）は、全件性を証明できないため人間判断へ停止する
（fail-closed、cap を無制限にはしない）。

**呼び出し側は `$?`（exit code）を continue/stop の分岐条件に使ってはならない**。分類に成功した場合はどの route でも exit 0 を返す（Major 2、下記参照）。route の正本は常に出力 JSON の `route` フィールドである:

```bash
ROUTE=$(uv run python3 -c "import json,sys; print(json.load(sys.stdin)['route'])" < /tmp/overlap_preflight_${ISSUE_NUMBER}.json)
```

このスクリプトは:
- `--issue-number` を必須にし、対象 Issue 自身を候補から自己除外する。
- `phase/implementation` ラベルが付いた OPEN Issue を `gh issue list` で列挙する（`number,title,body,labels,updatedAt,url`）。
- 全候補の本文から Allowed Paths をローカルで抽出する。`Allowed Paths` 未記載だけが schema error である候補は `ignored_missing_allowed_paths` として evidence に残し、collision classifier の candidate pool には渡さない。number / body / updatedAt / dependency contract の error を併発する候補は従来どおり fail-closed とする。
- 明示的な取得上限（`--limit`、既定 100）と saturation 検出を持ち、全件性を証明できない場合は fail-closed にする。
- Machine-Readable Contract の `blocked_by` / `depends_on` / `supersedes`（YAML list、inline/block 両表記）、legacy `Depends on #N` 記法、GitHub native dependency（`blockedBy` / `blocking`）を統合的に解析する。current が参照する predecessor が OPEN candidate 一覧に含まれない場合、オンライン経路では個別に readback して実 state（OPEN/CLOSED）を確認する。predecessor の実 state に基づき C2a（closed、直列化可能）と C2b（open、待機）を分岐する。
- 収集した comparable candidate JSON だけを `check_issue_overlap.py` の pure collision classifier に渡した上で、候補ごとに `## Outcome` / `## In Scope` を readback し、**構造的シグナル**（AC ID・output schema 名・Machine-Readable Contract の key/value・In Scope 内 edit target（inline-code パス）・goal_ref・supersedes/superseded-by）を主軸に意味的重複を判定する。自然言語類似度（Outcome の token Jaccard）は補助 signal に留め、`proceed_with_collision_evidence` を許可する唯一の根拠にはしない。
- `Allowed Paths` が同一集合であることは duplicate の十分条件にしない。`same_path_set` に基づく duplicate 候補は readback + 構造シグナルによる確認を経て初めて `duplicate` route を確定し、確認できない場合は C1 と同様に扱う。
- 全 candidate の number / body / updatedAt / dependency contract schema を検証し、一件でも欠ければ `human_review_required` に倒す（false positive での黙殺を防ぐ）。Allowed Paths 未記載だけは非比較対象として除外するため validation error に含めない。
- `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence（`current_issue` / `source` / `candidates`（candidate ごとの `policy_class` / `reasons` / `structural_signals`）/ `ignored_candidates`（`issue_number` と `reason: ignored_missing_allowed_paths`）/ `dependency_resolution` / `validation_errors` / `route` / `decision_inputs_sha256` / `evidence_sha256`）を標準出力に JSON で返す。

#### route / exit code 契約（クローズドセット、Major 2 改訂）

| route | 意味 | 本 Section の対応 |
|---|---|---|
| `proceed` | C0（重複候補なし） | 実装を継続する |
| `proceed_with_collision_evidence` | 証明済み C1/C2a（全候補 readback 完了かつ構造的に disjoint） | evidence を Issue コメントまたは worktree artifact に記録してから継続する |
| `wait_for_predecessor` | C2b（open predecessor への依存が検出された） | 人間判断へ停止（predecessor 完了待ち） |
| `human_review_required` | C3 / ambiguous / readback 不完全 / candidate schema 不備 / dependency 未解決 / source degraded（saturated 等） | 人間判断へ停止 |
| `duplicate` | readback で確認済みの重複 | 人間判断へ停止（統合 PR を人間に提案） |
| `runtime_error` | JSON parse 失敗 / schema 違反 / GitHub 取得失敗 | 人間判断へ停止（fail-closed） |

**exit code**: 分類処理が成功した場合（`route` が上記 closed set のいずれかに決定できた場合）は **route を問わずすべて exit 0** を返す。GitHub 取得失敗 / JSON・schema 破損時のみ `runtime_error` として exit 1 を返す。`set -e` を使うシェルでも継続 route（`proceed_with_collision_evidence` / `wait_for_predecessor` / `human_review_required` / `duplicate`）で意図せず停止しない。unknown な verdict / policy_class（`check_issue_overlap.py` の契約違反の兆候）は `runtime_error` に倒される。

- **継続 route（AC2）**: `proceed` と `proceed_with_collision_evidence` は実装を継続する。`proceed_with_collision_evidence` の場合、`IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence 全体を Issue コメントまたは worktree artifact に記録してから Step 3 へ進む。`open-pr` は同じ evidence digest（`evidence_sha256`）を PR 本文へ転記する。
- **fail-closed route（AC3）**: `wait_for_predecessor` / `human_review_required` / `duplicate` / `runtime_error` はいずれも実装を開始せず、人間判断へ停止する。route と evidence（またはエラー内容）を人間へ提示する。
- **candidate readback 前提（AC4）**: `check_implementation_overlap.py` は候補の `## Outcome` / `## In Scope` の readback が完了し、かつ構造的シグナルと自然言語類似度の双方から disjoint であることを確認できて初めて `proceed_with_collision_evidence` を返す。readback が不完全な候補が一件でもある場合、または構造的シグナルもしくは Outcome の意味的重複が検出された候補が一件でもある場合は `human_review_required` に倒す。**candidate contract の Outcome / In Scope / Out of Scope / Delivery Rule を readback する前に統合 PR を提案してはならない。** `Allowed Paths` の同一集合一致（`same_path_set`）だけでは duplicate と確定しない。
- **自己除外（AC6）**: `--issue-number` は必須であり、対象 Issue 自身は候補収集レイヤーによって自動的に自己除外される。自己除外を怠ると同一タイトル・同一 Allowed Paths によって `duplicate` と誤判定される。

#### 候補収集契約 collection contract（#1493、AC1/AC3）

`check_implementation_overlap.py` の evidence `source` には、GraphQL cursor
pagination の全件性を示す collection contract フィールドが additive で
含まれる: `collection_mode`（`exhaustive_cursor_pagination` 固定）、
`page_size`、`page_count`、`fetched_count`、`has_next_page`、`complete`、
`saturated`、`limit`（safety cap）。`open-pr` 側の overlap preflight hard
gate は、stored evidence と fresh（オンライン再実行）evidence の
collection contract が完全一致することを検証し、いずれかにこれらの
フィールドが欠けている場合（collection contract 未対応の legacy evidence）
は再収集を要求して fail-closed に拒否する。呼び出し元は `--limit` や
collection contract を上書きできない — 唯一の入力は integrity 確認済み
stored evidence の `source.limit`（safety cap）である。

#### PR 作成直前の deterministic drift gate（Major 1）

Step 7（push & PR 起票）の直前に、`route` が `proceed_with_collision_evidence` または `wait_for_predecessor` 解除直後だった場合は、`check_implementation_overlap.py` を **再実行**して stale evidence（`updated_at` / `body_sha256` drift）を確認する:

```bash
uv run --locked python3 .claude/skills/implement-issue/scripts/check_implementation_overlap.py \
  --issue-number "$ISSUE_NUMBER" \
  --repo "$REPO" \
  --limit 2000 \
  > /tmp/overlap_preflight_${ISSUE_NUMBER}_recheck.json
```

再実行後の `evidence_sha256`（または各 candidate の `body_sha256` / `updated_at`）が Step 2 実行時の値と異なる場合は drift と判定し、**本 Section を再実行してから Step 3 以降をやり直す**。drift が解消しない、または新たに `wait_for_predecessor` / `human_review_required` / `duplicate` に route が変わった場合は、Step 7 へ進まず（`git push` / `gh pr create` を呼ばず）人間判断へ停止する。これは deterministic gate であり、自然言語での「再確認する」という指示に留めない。

open-pr 側の validator が `overlap_preflight` evidence を強制検証する変更（`open-pr/scripts/update_pr.py` 等）は、本 Issue（#1452 / PR #1455）の Allowed Paths 外（follow-up 要）。現時点では本 SKILL.md の deterministic drift gate（上記）と、Step 7 で `gh pr create` を直接呼ばないこと（`open-pr` に委譲）が唯一の強制ポイントである。

`check_issue_overlap.py` 本体の scoring / schema ロジックの変更は本 Section の対象外（#1452 の Out of Scope）。また、本 preflight の continue 判定は OPEN Issue 間の意味的適合性のみを示し、active worktree / dirty path / 進行中 PR との同時編集安全性は証明しない（別 gate、#966 の責務）。

### 3. Worktree / Branch 作成手順

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

### 3.5. Runtime Verification Applicability（動作検証適用範囲）の確認

Issue 本文の `## Runtime Verification Applicability` を確認する。

- `decision: not_applicable` → runtime AC / VC / 証跡は不要。静的検証のみで実装を完結させる。
- `decision: immediate` → 動作検証 AC に対応する VC スクリプト（bash / pytest 等）と `artifacts/` 出力ロジックを実装する。証跡を PR 本文に添付する。実行環境が不可なら SKIP exit 77 を返す（SKIP = PASS ではない）。`deferred` の動作検証を捏造しない。
- `decision: deferred` → 後続 Issue / 統合フェーズ / 検証条件を PR 本文に引用するのみ。`deferred` の動作検証を本 Issue の実装中に捏造しない。証跡の提出は後続 Issue / フェーズで行う。
- `## Runtime Verification Applicability` セクション自体が存在しない場合は、`issue-contract-review` が go 判定を出す前に確認済みのはずだが、万が一セクションが存在しない状態で本 skill に到達した場合は **実装を開始せず人間にエスカレーション**する（`issue-refinement-loop` 経由で Issue 本文を更新させるか、呼び出し元に `status: blocked` を返す）。`not_applicable` と推定して実装を進めてはならない。

詳細は `docs/dev/runtime-verification-policy.md` の「Runtime Verification Applicability」を参照する。

### 4. TDD + BDD で実装

LOOP_PROTOCOL のテスト戦略に従う:

- **TDD**: 実装前に Vitest テストを書く（`tests/<対象>.test.ts`）
- **BDD**: テスト名は GIVEN/WHEN/THEN 形式
- 各 AC に対応するテストを少なくとも 1 つ書く
- 境界値（0、最大値、空入力）と異常系を含める

実装中の制約:
- **Allowed Paths 外を編集しない**（CLAUDE.md / per-directory CLAUDE.md の制約も遵守）
- スコープ外の改善・リファクタリングを混ぜない（別 Issue で扱う）
- `git add -A` / `git add .` を使わず、変更ファイルを明示してステージング

### 5. Verification Commands を実行

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

### 6. コミット

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

### 7. push & PR 起票（`open-pr` skill に委譲）

PR 起票は本 skill の責務外。`open-pr` skill に委譲する。

```bash
git push -u origin "$BRANCH"
```

push 完了後、以下を `open-pr` skill に渡して起票させる:

- `linked_issue`: `$ISSUE_NUMBER`
- `pr_title`: `<type>: <subject>`
- `contract_snapshot_url`: 受け取った contract-snapshot comment URL
- `verification_summary`: ステップ 5 で記録した PASS / FAIL サマリ
- `allowed_paths_compliance`: true / false
- `overlap_preflight`（Step 2 の route が `proceed_with_collision_evidence` だった場合のみ必須。それ以外は省略可）:
  ```yaml
  overlap_preflight:
    required: true
    evidence_file: /tmp/overlap_preflight_<ISSUE_NUMBER>_recheck.json  # Major 1 drift gate 再実行後のファイル
    expected_digest: sha256:<evidence_sha256>
  ```

PR 本文テンプレ・publish ゲート・idempotency チェック・`Closes`/`Refs` の使い分けは `open-pr` 側の責務。本 skill では `gh pr create` を直接呼ばない。`overlap_preflight` を open-pr 側が強制検証する validator 配線は #1452 / PR #1455 の Allowed Paths 外であり follow-up とする（PR 作成直前の deterministic drift gate の項を参照）。

### 8. Issue コメントへの結果報告

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
- `.claude/skills/create-issue/scripts/check_issue_overlap.py` — 本 skill Step 2 が再利用する pure overlap classifier の正本
- `.claude/skills/implement-issue/scripts/check_implementation_overlap.py` — 本 skill Step 2 が実行する implementation 専用 overlap preflight adapter
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
  reason_code: null | expected_head_sha_missing | expected_head_sha_mismatch | permission_denied | secondary_rate_limit | validation_failed | head_unchanged_after_accepted | transport_error | unknown_http_status
  update_method: merge_only  # リクエストの update_method を echo（検証用）
  http_status: 202 | 403 | 422 | 429 | <other>
  before_head_sha: <sha>
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
  rate_limit_diagnostics:  # secondary_rate_limit 時のみ
    retry_after_seconds: <int | null>
    x_ratelimit_remaining: <int | null>
    x_ratelimit_reset: <epoch | null>
  error_body: <string>   # 分類根拠の body
  errors: []
```

### 呼び出し形式

```bash
set -euo pipefail

REPO=$(git remote get-url origin | sed 's/.*github.com[:/]//' | sed 's/\.git$//')
PR_NUMBER=<番号>
EXPECTED_HEAD_SHA=<reviewed_head_sha>

# gh api -i でヘッダと body を切り分ける
# 出力形式: HTTP/X.X <status> <reason>\n<headers>\n\n<body>
UPDATE_RESPONSE=$(gh api -i -X PUT "repos/$REPO/pulls/$PR_NUMBER/update-branch" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -f expected_head_sha="$EXPECTED_HEAD_SHA" 2>&1 || true)

# HTTP status 行を抽出
HTTP_STATUS=$(echo "$UPDATE_RESPONSE" | head -n1 | grep -oE '[0-9]{3}' | head -n1)
# body 部分（空行以降）を抽出
RESPONSE_BODY=$(echo "$UPDATE_RESPONSE" | awk '/^\r?$/{found=1; next} found{print}')
```

`gh pr update-branch` は使用しない（`expected_head_sha` オプションがないため）。

本 contract は **REST merge update 固定**（`update_method: merge_only`）。linear history またはリベース必須リポジトリは out-of-scope であり、403 / 422 / 429 / transport error は `UPDATE_BRANCH_RESULT_V1` の reason_code に決定論的に正規化する。GraphQL `updatePullRequestBranch` mutation および rebase update は本 contract 対象外（Out of Scope）。

### HTTP ステータス別分岐

**202 Accepted（リクエスト受理）:**

update が受け付けられた。headRefOid が `EXPECTED_HEAD_SHA` から変化するまで poll する（bounded retry: 5 秒 × 最大 12 回）:

```bash
POLL_MAX=12
POLL_INTERVAL=5
UPDATED=false  # 決定論的フラグ（空文字誤認防止）

for i in $(seq 1 $POLL_MAX); do
  NEW_HEAD=$(gh pr view "$PR_NUMBER" --json headRefOid --jq .headRefOid)
  if [ -n "$NEW_HEAD" ] && [ "$NEW_HEAD" != "$EXPECTED_HEAD_SHA" ]; then
    UPDATED=true
    break
  fi
  sleep $POLL_INTERVAL
done
```

- `UPDATED=true` → `UPDATE_BRANCH_RESULT_V1.status: ok`、`new_head_sha: $NEW_HEAD` を記録
- `UPDATED=false`（bounded retry 上限到達）→ `status: failed` / `reason_code: head_unchanged_after_accepted`

**403 Forbidden（権限拒否）:**

権限不足またはフォーク PR の書き込み制限。以下の `permission_diagnostics` を出力して `status: permission_blocked` / `reason_code: permission_denied` とする:

```bash
# auth_actor の確認
gh api user --jq .login

# PR の fork / maintainer_can_modify 確認
gh pr view "$PR_NUMBER" --json headRepository,maintainerCanModify \
  --jq '{head_repo: .headRepository.nameWithOwner, maintainer_can_modify: .maintainerCanModify}'
```

403 時は `UPDATE_BRANCH_RESULT_V1.permission_diagnostics` に auth_actor、head_repo、base_repo、fork_pr、maintainer_can_modify、required_permissions を含めて記録する。`required_permissions` には `pull_requests:write` と `contents:write_on_head_repository_when_github_app` の両方を明記する。

**422 Unprocessable Entity（処理不可）:**

body 内容で分類する（422 全体を `expected_head_sha` mismatch とは断定しない）:

| body の内容 | status |
|---|---|
| `expected_head_sha` mismatch | `expected_head_sha_mismatch` — Step 4 re-review 後 Step 5 再実行 |
| secondary rate limit | `secondary_rate_limit` — fail-closed。header 由来の diagnostics を返して再実行判断を人間へ委譲 |
| その他 validation failure | `validation_failed` |

### Bash 許可例外

`gh api -X PUT repos/{owner}/{repo}/pulls/{pull_number}/update-branch` の実行は `implementation-worker`（`.claude/agents/implementation-worker.md`）に許可された Bash 操作例外に含まれる。

## IMPLEMENTATION_WORKER_REQUEST_V2 対応

`impl-review-loop` から `IMPLEMENTATION_WORKER_REQUEST_V2` を受け取った場合、worker は PR repair executor として動作する。詳細スキーマ・routing table・各 mode の挙動は `.claude/agents/implementation-worker.md` の `IMPLEMENTATION_WORKER_REQUEST_V2` セクションを参照すること。

### update_pr_body_hygiene（PR body 更新）

`update_pr_body_hygiene` mode では `open-pr/scripts/update_pr.py` wrapper 経由での実行を必須とする。
**`gh pr edit --body-file` の直接呼び出しは本 SKILL.md からも禁止**（wrapper 内部実装としての使用は例外）。

### update_branch（PR ブランチ更新）

`update_branch` mode は本 SKILL.md の `UPDATE_BRANCH_REQUEST_V1` contract を使用する（上記セクション参照）。
`IMPLEMENTATION_WORKER_REQUEST_V2.expected_head_sha` が未指定の場合は `UPDATE_BRANCH_REQUEST_V1` を発行せず `status: blocked` を返す。
