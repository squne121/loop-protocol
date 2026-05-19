---
name: implement-issue
description: 承認済みの implementation child issue（`issue-contract-review` で go 判定済み）を、Allowed Paths 内で実装し、Verification Commands で検証し、Draft PR を作成して Issue コメントに結果を返すまでを `1 Issue = 1 PR` で進める手順。「Issue ◯◯ 実装して」「implement issue」「この Issue やって」のトリガーで使う。
---

# Implement Issue

承認済み contract に従い、implementation child issue を実装し、verify、PR、Issue 更新まで進める手順。
`issue-contract-review` で `status: go` を得た後に呼ぶ。

## Input

- `Issue番号` または `Issue URL`（必須）
- `issue-contract-review` の contract-snapshot comment URL（必須）

## Use When

- `issue-contract-review` が完了し、人間が Go を返した後
- implementation child issue を Allowed Paths 内で実装したい
- PR 本文と linked issue comment を primary surface にしたい

## Procedure

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

ready tuple（title `実装:` + `phase/implementation` + `state/queued`）が揃っているかも確認。不一致なら停止して人間判断を仰ぐ。

### 2. 複数 Issue 同時着手時の Allowed Paths 重複チェック

本 Issue の Allowed Paths と、現在 OPEN の他 implementation Issue の Allowed Paths が重複する場合、マージコンフリクトのリスクがあるため統合 PR を提案する。

```bash
# 同じファイルを Allowed Paths に持つ open Issue を検索
for path in $(awk '/^## Allowed Paths$/{flag=1;next} /^## /{flag=0} flag && /^- /' issue_body.md | sed 's/^- //'); do
  gh issue list --search "\"$path\" is:open" --state open --json number,title --jq '.[] | select(.number != '"$ISSUE_NUMBER"')'
done
```

重複あり → 統合 PR への切替を人間に提案して停止。重複なし → 次へ。

### 3. Worktree / Branch 作成

```bash
SLUG="<short-slug>"  # contract-snapshot の Worktree フィールドから取得
WORKTREE=".claude/worktrees/issue-${ISSUE_NUMBER}-${SLUG}"
BRANCH="worktree-issue-${ISSUE_NUMBER}-${SLUG}"

git worktree add "$WORKTREE" -b "$BRANCH" main
cd "$WORKTREE"
```

- **配置先は必ず `.claude/worktrees/` 配下**（リポジトリ外配置禁止 — workspace trust prompt 再発防止）
- 既存衝突は `issue-contract-review` で検出済みのため、ここで衝突した場合は人間判断を仰ぐ

worktree 内で Edit / Write する際は **必ず worktree 内の絶対パス**を指定する。main の絶対パスを指定すると main のファイルが変更される事故が起きる。

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

### 7. push & Draft PR 作成

```bash
git push -u origin "$BRANCH"

gh pr create --repo "$REPO" --base main --draft \
  --title "<type>: <subject>" \
  --body-file /tmp/pr-body-${ISSUE_NUMBER}.md
```

PR 本文テンプレ（`/tmp/pr-body-${ISSUE_NUMBER}.md` に書き出す）:

```markdown
## Summary
- <変更内容の要点 1>
- <変更内容の要点 2>

## 受け入れ条件の達成状況
- [x] AC1: <達成（根拠）>
- [x] AC2: <達成（根拠）>

## 検証コマンド結果
- `pnpm typecheck`: ✅ 通過
- `pnpm lint`: ✅ 通過
- `pnpm test`: ✅ 通過（X files / Y tests）
- `pnpm build`: ✅ 通過

## Allowed Paths 遵守
- 変更ファイルがすべて Allowed Paths 内であることを確認済み

## 関連
Closes #<ISSUE_NUMBER>
Contract Snapshot: <comment URL>
```

### 8. Issue コメントへの結果報告

```bash
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body "## implement-issue: 実装完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- PR: <PR URL>
- Worktree: \`$WORKTREE\`
- Branch: \`$BRANCH\`
- Verification: 4/4 PASS
- 後続: PR レビュー（pr-review-judge）→ マージ → post-merge-cleanup"
```

## Output (IMPLEMENT_RESULT_V1)

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

## Guardrails

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

## Related

- `.claude/skills/issue-contract-review/SKILL.md` — 着手前 preflight（本 skill の前段）
- `.claude/skills/impl-review-loop/SKILL.md` — 実装→検証→PR レビュー の 4 段ループ（オーケストレーター）
- `.claude/skills/open-pr/SKILL.md` — PR 起票手順（C-4 で整備予定）
- `.claude/skills/post-merge-cleanup/SKILL.md` — PR マージ後の cleanup
- `.claude/skills/ssot-discovery/SKILL.md` — 実装着手前の SSOT 探索
- `.claude/agents/implementation-worker.md` — 本 skill を使う SubAgent
- `.claude/agents/test-runner.md` — Verification Commands を実行する SubAgent
- ルート `CLAUDE.md` + per-directory `CLAUDE.md` — 不変条件の正本
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界
