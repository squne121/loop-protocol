---
name: issue-contract-review
description: GitHub の implementation child issue に着手する前、Issue contract の不足確認、Allowed Paths 確認、`1 Issue = 1 PR` 判定、split / scope delta / execution plan 固定が必要なときに使う。branch や PR を作る前に使う。
required_rules:
  - github-ops-workflow
  - issueops-common-guard
  - issue-body-ssot-policy
  - issueops-mode-guard
  - git-policy
---

# Issue Contract Review

implementation child issue を「このまま 1 PR に閉じてよいか」を判定するための skill。
実装そのものには入らず、Issue 本文と comment を contract-first に整える。

## Input

- `Issue番号` または `Issue URL`（必須）

## Use When

- implementation child issue の着手前
- `Outcome` `Acceptance Criteria` `Allowed Paths` `Required Skills` が不足している
- `1 Issue = 1 PR` に収まるかを判断したい
- scope delta や split が必要か確認したい
- 「Issue ◯◯ の contract 確認して」「着手前チェック」「contract review」などの短文トリガー

## Procedure

1. 入力を確定する:
   - `Issue番号` または `Issue URL` を受け取る。
   - parent issue、最新の human answer、最新 AI handoff を確認する。
2. contract を正規化する:
   - `.github/ISSUE_TEMPLATE/github-ops-implementation.md` を読み、`## ` で始まる必須セクション一覧を取得する。
   - 取得したセクション一覧の有無を Issue 本文で確認する（テンプレートが更新されても自動追従する）。
   - **`## Required Skills` の判断基準チェック**: `## Required Skills` セクションが存在する場合、`issue-contract-review` / `implement-issue` / `pr-review-judge` 等の暗黙的ワークフロースキルが列挙されていれば警告する（Blocking ではない）。runtime dependency のみを記載するよう Issue 本文の修正を提案する（詳細: `.agents/rules/github-ops-workflow.md` KH-N6）。
     > **注記（template mismatch fail-closed の現状）**: Issue 作成時の自動警告機能は未整備であり、create-issue の Issue Template Guard（生成時 guard）と issue-contract-review の BLOCKED（着手前チェック）が現時点の主な手段である。
   - **Blocking チェック（`## Verification Commands` 欠落）**: `## Verification Commands` セクションが Issue 本文に存在しない、またはセクションが空欄（コマンドが1つも記載されていない）の場合は **Blocking** とみなす。以下を Issue comment に書いて停止する:
     ```
     ## Contract Review: BLOCKED — Verification Commands 欠落
     `## Verification Commands` セクションが欠落または空欄です。
     Terminal AI Agent が AC ごとの検証を自己完結で実施できないため、実装 phase に進めません。

     ### 必要な対応
     - [ ] 各 AC に対応する実行可能コマンドを `## Verification Commands` に追記してください。
           例: `grep -n "keyword" path/to/file` / `just check <target>` など
     ```
2.4. **architecture-fit 事前チェック**（Allowed Paths が記載されている場合に実施）:
   - Issue の `Acceptance Criteria` と `Allowed Paths` を参照し、以下の観点で architecture 妥当性を確認する:
     - **ad-hoc directory 新設要求**: AC が `.agents/runtime/`, `.agents/cache/`, `.agents/tmp/` 等、CLAUDE.md §5「参照先」に記載されていないディレクトリの新設を要求していないか
       - 該当する場合: 「project convention（CLAUDE.md §5）で定義された正規ディレクトリへの配置で代替できないか」を確認し、代替案を Issue comment で提案すること（Blocking ではなく Warning）
     - **project tooling 非利用**: AC が `just` / `pyproject.toml` / `uv` 等の既存 tooling を使わない ad-hoc な bash 実装を要求していないか
       - 該当する場合: 既存 tooling による代替案を提案すること（Blocking ではなく Warning）
     - **AI 製品仕様外 registry**: AC が `.yaml` / `.json` registry を skill / script が直接 parse する構造を要求していないか
       - 該当する場合: Warning として記録し、adversarial-reviewer が実装後に `[ARCH-FIT] HIGH` で検出する対象として予告すること
   - 上記の Warning がある場合は contract-snapshot コメントに `## Architecture-Fit Warning` セクションを追加して記録する。人間の承認を妨げる Blocking ではない。

2.5. Issue 本文の Rules セクションを読む（存在する場合）:
   - Issue 本文に `## Rules` セクションがあれば、列挙された `.agents/rules/<file>` をすべて Read する
   - contract review 時に参照すべき制約・判断基準を把握する
3. `1 Issue = 1 PR` 判定を行う:
   - AC が独立せず複数責務に分かれる
   - Allowed Paths が別領域へ広がる
   - child issue を分けた方が review / rollback しやすい
   のいずれかがあれば split を提案する。
4. split または scope delta が必要なら停止する:
   - `templates/github-ops/contract-snapshot.md`
   - `templates/github-ops/scope-delta.md`
   を使い、Issue comment に不足点、分割案、必要な人間判断を書く。
   - split で新 child issue を起票した場合は、起票直後に sub-issue として parent に紐づける。
     登録手順の正本は `create-issue` スキルの「親子 Issue 構造ルール」セクションを参照する（sub-issue 登録の責務は `create-issue` が持つ）。
   - 起票後、以下のコマンドで sub-issue として登録済みかを確認する:
     ```bash
     # child issue の parent を read-back で確認する
     gh api repos/{owner}/{repo}/issues/{child_number}/parent --jq '.number'
     # 期待値: parent_number と一致する整数が返ること
     ```
     - 返り値が `parent_number` と一致すれば登録済み。
     - `404` / `410` またはその他の非 `200` の場合は「未紐付け」とみなし、fail-closed で停止する。`child_number` と `child_url` を人間へ報告し、`create-issue` の手順に従って手動登録を依頼する。
4.5. GUI 操作コードを含む Issue の live-verify 確認:
   - 変更対象コードが `uiautomation` / `pywinauto` 等の Windows GUI 操作を含む場合、または Windows GUI を操作するスクリプトを変更する場合は、以下を確認する:
     - `## Verification Commands` セクションに `# live-verify: required` マーカーが記載されているか確認する。
     - 記載がない場合は **Blocking** とみなす。以下を Issue comment に書いて停止する:
       ```
       ## Contract Review: BLOCKED — live-verify: required マーカー欠落
       GUI 操作コード（`uiautomation` / `pywinauto` 等）を変更する Issue には、
       `## Verification Commands` セクションに `# live-verify: required` マーカーの記載が必須です。

       ### 必要な対応
       - [ ] `## Verification Commands` セクションに以下を追記してください:
             `# live-verify: required`
       - [ ] または GUI 操作コードを変更しないことを AC に明記してください。
       ```
   - Required Skills に `windows-gui-dev` が含まれる場合も同様のチェックを実施する。

5. contract が十分なら execution plan を固定する:
   - `templates/github-ops/contract-snapshot.md`
   - `templates/github-ops/execution-plan.md`
   を使い、`Decision Replay Commands` と `Blocked Conditions` を残す。
6. 人間承認待ちで止まる:
   - 承認前に branch、PR、repo 編集へ進まない。

## Output Contract

**GitHub surface: child issue comment（新規追加）**

| ケース | 書くもの | 停止 |
|--------|----------|------|
| go | `contract-snapshot.md` + `execution-plan.md` をコメントに書く | 人間承認待ちで停止 |
| split / stop | scope-delta または split 提案をコメントに書く | 人間判断待ちで停止 |
| blocked | 下記 fail-closed 例をコメントに書く | 人間判断待ちで停止 |

### blocked（branch / PR 作れない）fail-closed 例

Issue comment に以下を書いて停止する:

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

## Handoff to implement-issue

`issue-contract-review` が go を返したとき、`implement-issue` へ渡す必須項目:

- Issue 番号
- contract-snapshot comment URL
- Outcome
- Acceptance Criteria（番号付き）
- Verification Commands
- Allowed Paths
- Required Skills
- Execution Plan（execution-plan.md の記録 URL）

## EnterWorktree ブランチ名注記（T-4）

Claude Code の `EnterWorktree` ツールはブランチ名の `/` を `.` に変換する。
例: `feat/issue-158-xxx` → `worktree-feat.issue-158-xxx`

Branch 命名フィールドを検証する際は、`EnterWorktree` を使用した場合の実体ブランチ名（変換後）と、Issue 指定名の両方を確認する。
乖離がある場合は PR 本文に対応関係を明示する（詳細は `.agents/rules/git-policy.md` §4.5 参照）。

## In Scope 解釈指針

### 基本原則: In Scope リストは「既知箇所の列挙」である

Issue の `In Scope` リストは、**Issue 作成時点での既知箇所を列挙したもの**であり、リストにない箇所を意図的に除外したことを意味しない。

AI エージェントはこの原則を厳守し、In Scope に明記されていない変更を機械的に「scope 外」と判定してはならない。

### 前提条件（本指針の適用可否）

**本指針は `Allowed Paths` と `In Scope` が Issue に記載されている場合にのみ適用する。**

| 前提条件 | 欠落時の扱い |
|----------|-------------|
| `Allowed Paths` が Issue に存在しない | 本指針を適用せず **blocked** として扱い、`Allowed Paths` の明記を求める |
| `In Scope` が空欄 | 列挙ベースの判定を行わず、人間に確認する |

### scope 内と解釈できる条件（AND 条件）

以下の **3 条件をすべて満たす**変更は、In Scope に明記がなくても **scope 内**と解釈する:

| 条件 | 判断基準 |
|------|----------|
| 同一ファイル | In Scope に記載されているファイルと**同一ファイル**である（ディレクトリ単位の拡大解釈をしない） |
| 同一意図 | Issue の `Outcome` / `Acceptance Criteria` を満たすために**直接必要**な変更である（Outcome と矛盾しないだけでは不十分） |
| Allowed Paths 内 | `Allowed Paths` セクションに記載されているパスに含まれる |

### AI レビューが「scope 外」と判定した場合の判断フロー

```
AI レビューが scope 外と判定
        │
        ▼
前提条件を確認: Allowed Paths / In Scope が両方 Issue に存在するか？
        │
  存在しない → blocked: 前提条件の明記を求めて停止
        │
  存在する
        ▼
上記 3 条件（同一ファイル・同一意図・Allowed Paths 内）をすべて満たすか？
        │
   YES  │  NO
        │   └─→ scope 外として扱い、既存の scope delta / revert 判断へ進む
        ▼
人間に確認してからスコープ判断する
（自動で scope 外 / revert / REQUEST_CHANGES しない）
        │
        ▼
人間が「scope 内」と判断 → 変更を維持する
人間が「scope 外」と判断 → revert または follow-up Issue に切り出す
```

### 注意事項

- AI レビュー（Codex・Gemini 等）が「In Scope 外」と判定しても、上記 3 条件を満たす場合は **自動で revert・REQUEST_CHANGES・follow-up Issue 起票を行わない**。
- 3 条件を満たすかどうか自体が曖昧な場合も、自動判断せず人間に確認する。
- この解釈指針は `issue-contract-review`（着手前）での適用を主目的とする。`pr-review-judge`（レビュー時）への適用は意図されているが、review skill 側の対応は #457 で追跡中であり、実装完了まで `pr-review-judge` での適用は人間の判断に委ねる。

## 追加ガイダンス（issue-contract-review）

### fixture / spec-contract fixture 漏れ確認

- `spec-contract` または `fixture` を含む Issue の review では、`Allowed Paths` が fixture 側を含む場合のみ合格にしない。  
  Issue 本文が想定する仕様変更に対して、`fixture` 参照差分が抜けていないかを確認する。
- `Allowed Paths` と AC を突合し、`spec-contract` と `fixture` のどちらかが欠ける場合は `scope delta` / `split` の前提で BLOCK/stop 対象として明示する。

## Guardrails

- code 編集を開始しない。
- Issue 本文にない完了条件を黙って追加しない。
- current session や repo policy が branch / PR と衝突する場合は、その事実を contract 上の blocker として返す。
- `Allowed Paths` が曖昧なまま実装 phase に進めない。
- In Scope に明記されていない変更を、条件確認なしに機械的に「scope 外」と判定しない（詳細は「In Scope 解釈指針」を参照）。

## Related

- rule: `.agents/rules/github-ops-workflow.md`
- rule: `.agents/rules/issueops-common-guard.md`
- policy: `.kiro/steering/github-ops-loop.md`
- skill: `.agents/skills/create-issue/SKILL.md`（sub-issue 登録手順の正本。split 時の登録手順はこちらを参照）
- template: `templates/github-ops/contract-snapshot.md`
- template: `templates/github-ops/execution-plan.md`
- issue-template: `.github/ISSUE_TEMPLATE/github-ops-implementation.md`
