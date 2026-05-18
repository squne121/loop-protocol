---
name: issue-contract-review
description: GitHub の implementation child issue に着手する前、Issue contract（Outcome / AC / Allowed Paths）の不足確認、`1 Issue = 1 PR` 判定、split / scope delta / execution plan 固定が必要なときに使う。branch や PR を作る前に必ず通す preflight。
---

# Issue Contract Review

implementation child issue を「このまま 1 PR に閉じてよいか」を判定する preflight skill。
実装そのものには入らず、Issue 本文と comment を contract-first に整える。

## Input

- `Issue番号` または `Issue URL`（必須）

## Use When

- implementation child issue の着手前
- `Outcome` / `Acceptance Criteria` / `Allowed Paths` / `Required Skills` が不足している
- `1 Issue = 1 PR` に収まるかを判断したい
- scope delta や split が必要か確認したい
- 「Issue ◯◯ の contract 確認して」「着手前チェック」「contract review」などの短文トリガー

## Procedure

1. 入力を確定する:
   - `Issue番号` または `Issue URL` を受け取る
   - parent issue、最新の human answer、最新 AI handoff を確認する

2. contract を正規化する:
   - `.github/ISSUE_TEMPLATE/implementation.yml` を読み、各 textarea の `label` を必須セクション一覧として取得する（テンプレ更新時に自動追従）
   - 取得したセクション一覧の有無を Issue 本文で確認する
   - **`## Required Skills` の判断基準チェック**: セクションが存在する場合、ワークフロー skill（`issue-contract-review` / `implement-issue` / `pr-review-judge` / `ssot-discovery` 等）が列挙されていれば警告する（Blocking ではない）。ドメイン知識 skill（TypeScript / ECS / Canvas 等）のみを記載するよう本文の修正を提案する
   - **Blocking チェック（`## Verification Commands` 欠落）**: セクションが存在しないか空欄の場合は **Blocking**。Issue comment に書いて停止する:
     ```
     ## Contract Review: BLOCKED — Verification Commands 欠落
     `## Verification Commands` セクションが欠落または空欄です。
     Terminal AI Agent が AC ごとの検証を自己完結で実施できないため、実装 phase に進めません。

     ### 必要な対応
     - [ ] 各 AC に対応する実行可能コマンドを `## Verification Commands` に追記してください
           例: `grep -n "keyword" path/to/file` / `pnpm typecheck && pnpm lint && pnpm test && pnpm build` など
     ```

3. **architecture-fit 事前チェック**（Allowed Paths が記載されている場合）:
   - Issue の `Acceptance Criteria` と `Allowed Paths` を参照し、project convention（`CLAUDE.md` ルート + per-directory `CLAUDE.md`）と照合する
   - **ad-hoc directory 新設要求**: AC が `CLAUDE.md` に記載されていないディレクトリの新設を要求していないか
   - **既存 tooling 非利用**: AC が `pnpm` / `vite` / `vitest` 等の既存 tooling を使わない ad-hoc な実装を要求していないか
   - 該当する Warning は Issue comment の `## Architecture-Fit Warning` セクションに記録する（Blocking ではない）

4. **`1 Issue = 1 PR` 判定** を行う。以下のいずれかに該当する場合は split を提案する:
   - AC が独立せず複数責務に分かれる
   - Allowed Paths が `src/state` ↔ `src/render` などアーキテクチャ境界をまたぐ
   - child issue を分けた方が review / rollback しやすい

5. split または scope delta が必要なら停止する:
   - Issue comment に不足点・分割案・必要な人間判断を書く（テンプレ → 後述 Output Contract 参照）
   - split で新 child issue を起票した場合は、起票直後に sub-issue として parent に紐づける（sub-issue 登録の責務は `create-issue` が持つ）
   - 起票後、以下で sub-issue として登録済みかを read-back 確認する:
     ```bash
     gh api repos/{owner}/{repo}/issues/{child_number}/parent --jq '.number'
     # 期待: parent_number と一致する整数
     ```
     - 非 `200` の場合は「未紐付け」とみなし fail-closed で停止。`child_number` と `child_url` を人間へ報告し、`create-issue` の手順に従って手動登録を依頼する

6. contract が十分なら execution plan を固定する（後述 Output Contract の `execution-plan` セクション参照）

7. 人間承認待ちで止まる:
   - 承認前に branch、PR、repo 編集へ進まない

## In Scope 解釈指針

### 基本原則

Issue の `In Scope` リストは **Issue 作成時点での既知箇所を列挙したもの**であり、リストにない箇所を意図的に除外したことを意味しない。
AI は本原則を厳守し、In Scope に明記されていない変更を機械的に「scope 外」と判定してはならない。

### 前提条件

本指針は `Allowed Paths` と `In Scope` が両方記載されている場合にのみ適用する:

| 前提条件 | 欠落時の扱い |
|---|---|
| `Allowed Paths` が存在しない | 本指針を適用せず **blocked** として扱い、明記を求める |
| `In Scope` が空欄 | 列挙ベースの判定を行わず、人間に確認する |

### scope 内と解釈できる条件（AND）

以下の **3 条件をすべて満たす**変更は、In Scope に明記がなくても scope 内と解釈する:

| 条件 | 判断基準 |
|---|---|
| 同一ファイル | In Scope に記載されているファイルと**同一ファイル**である（ディレクトリ単位の拡大解釈をしない） |
| 同一意図 | Issue の `Outcome` / `Acceptance Criteria` を満たすために**直接必要**な変更 |
| Allowed Paths 内 | `Allowed Paths` に記載されているパスに含まれる |

### 判定不能時の挙動

3 条件を満たすかどうか曖昧な場合も、AI は自動判断せず人間に確認する。
本指針は `issue-contract-review`（着手前）での適用を主目的とする。`pr-review-judge` への適用は人間の判断に委ねる。

## Output Contract

GitHub surface: child issue comment（新規追加）

| ケース | 書くもの | 停止 |
|---|---|---|
| go | contract-snapshot + execution-plan をコメントに書く | 人間承認待ちで停止 |
| split / stop | scope-delta または split 提案をコメントに書く | 人間判断待ちで停止 |
| blocked | 下記 fail-closed 例をコメントに書く | 人間判断待ちで停止 |

### contract-snapshot コメントの最小構成

```markdown
## Contract Snapshot

- **Outcome**: <Issue 本文の Outcome を 1 文で>
- **Acceptance Criteria**:
  1. <AC1>
  2. <AC2>
- **Verification Commands**: <列挙>
- **Allowed Paths**: <列挙>
- **Required Skills**: <ドメイン知識 skill のみ>
- **Stop Conditions**: <Issue 本文を再掲>
```

### execution-plan コメントの最小構成

```markdown
## Execution Plan

- **Worktree**: `.claude/worktrees/issue-<番号>-<slug>`
- **Branch**: `worktree-issue-<番号>-<slug>`
- **Decision Replay Commands**: <主要 git/gh コマンドを順序で>
- **Blocked Conditions**: <作業停止条件>
```

### blocked（branch / PR 作れない）fail-closed 例

```markdown
## Contract Review: BLOCKED
current session policy により branch / PR を作成できません。

### Contract（確定済み）
- Outcome: <value>
- Acceptance Criteria:
  1. <AC1>
  2. <AC2>
- Verification Commands: <list>
- Allowed Paths: <list>

### 人間への要求
- [ ] branch 作成制約を緩和する（worktree 運用 Issue を先に完了させる等）
- [ ] または main 直コミット運用へ切り替える判断を返す
```

## Handoff to implement-issue

`issue-contract-review` が go を返したとき、`implement-issue` へ渡す必須項目:

- Issue 番号
- contract-snapshot comment URL
- Outcome
- Acceptance Criteria（番号付き）
- Verification Commands
- Allowed Paths
- Required Skills
- Execution Plan（execution-plan コメント URL）

## EnterWorktree ブランチ名注記

Claude Code の `EnterWorktree` ツールはブランチ名の `/` を `.` に変換する。
例: `feat/issue-42-foo` → `worktree-feat.issue-42-foo`

Branch 命名フィールドを検証する際は、`EnterWorktree` を使用した場合の実体ブランチ名（変換後）と Issue 指定名の両方を確認する。

## Guardrails

- code 編集を開始しない
- Issue 本文にない完了条件を黙って追加しない
- current session や repo policy が branch / PR と衝突する場合は、その事実を contract 上の blocker として返す
- `Allowed Paths` が曖昧なまま実装 phase に進めない
- In Scope に明記されていない変更を、条件確認なしに機械的に「scope 外」と判定しない（「In Scope 解釈指針」を参照）

## Related

- `.claude/skills/create-issue/SKILL.md` — sub-issue 登録手順の正本
- `.claude/skills/implement-issue/SKILL.md` — go 後の handoff 先
- `.claude/skills/ssot-discovery/SKILL.md` — Issue 関連 SSOT の探索
- `.github/ISSUE_TEMPLATE/implementation.yml` — 必須セクション一覧の正本
- ルート `CLAUDE.md` + per-directory `CLAUDE.md` — architecture-fit 判定の規範
