# Agent / Skill 責務境界

LOOP_PROTOCOL の Issue 駆動開発で使う各 skill / SubAgent の責務境界を、開発者が運用上参照するためのドキュメント。
SKILL.md 本文には書かず（コンテクスト汚染を避けるため）、本ドキュメントを正本とする。

## Issue 管理系

| skill / agent | 役割 | 入力 | 出力 |
|---|---|---|---|
| `create-issue` skill | 新規 Issue 起票 | ユーザー要求 | 新規 Issue（`gh issue create`） |
| `issue-author` SubAgent | 既存 Issue 本文の更新 | issue_number + reviewer_feedback_url | 更新後の Issue 本文（`gh issue edit`） |
| `review-issue` skill | Issue 本文の構造的品質チェック | issue_number | 修正差分提案 + verdict（approve / needs-fix） |
| `issue-contract-review` skill | 実装着手前の contract 確認（AC / Allowed Paths / 1 PR 判定） | issue_number | contract-snapshot + execution-plan、または split / blocked 提案 |
| `issue-refinement-loop` skill | Issue 改善 4 段ループのオーケストレーター | issue_number | iteration 終了状態 |

## 実装系（C-2 で適合予定）

| skill / agent | 役割 |
|---|---|
| `implement-issue` skill | 承認済み implementation issue を 1 PR で完了させる |
| `implementation-worker` SubAgent | 実装 worker（implement-issue から委譲） |
| `test-runner` SubAgent | Verification Commands を実行して AC ごとに PASS/FAIL 報告 |

## レビュー系（C-3 で適合予定）

| skill / agent | 役割 |
|---|---|
| `pr-review-judge` skill | PR の review 判定（APPROVE / REQUEST_CHANGES） |
| `pr-reviewer` SubAgent | PR review worker |

## オーケストレーション系（C-4 で適合予定）

| skill / agent | 役割 |
|---|---|
| `impl-review-loop` skill | 実装→検証→PR レビュー の 4 段ループ |
| `open-pr` skill | PR 起票 |
| `post-merge-cleanup` skill | PR マージ後の cleanup |
| `post-merge-cleanup-worker` SubAgent | cleanup worker |

## 補助系

| skill / agent | 役割 |
|---|---|
| `ssot-discovery` skill | `docs/` 配下を SSOT として横断探索 |
| `codebase-investigator` SubAgent | 大規模コードベース調査 |
| `gemini-cli-headless-delegation` skill | Gemini CLI への headless 委譲 |
| `nlm-skill` skill | NotebookLM CLI / MCP 操作（既存導入） |

## 設計原則

### review-issue と issue-contract-review の使い分け

- `review-issue`: Issue 本文の **構造的品質**（AC が検証可能か、Outcome に成果物形式があるか、Required Skills 意味論を満たすか等）
- `issue-contract-review`: 着手直前の **contract 確認**（Allowed Paths と AC の整合、1 PR 判定、execution plan 固定）
- 順序: `review-issue`（本文整備）→ `issue-contract-review`（実装着手前 preflight）→ `implement-issue`（実装）

### create-issue と issue-author の使い分け

- `create-issue` skill: **新規** Issue を起票する。`gh issue create` を呼ぶ
- `issue-author` SubAgent: **既存** Issue の本文を更新する。`gh issue edit` を呼ぶ
- 2 つは入力も出力も異なり、相互呼び出しもしない（issue-author は create-issue を使わない）

### SubAgent vs Skill の責務分離

| 区分 | Skill | SubAgent |
|---|---|---|
| **責務** | 再現可能な作業手順 | 隔離コンテクストでの実行 |
| **格納先** | `.claude/skills/<name>/SKILL.md` | `.claude/agents/<name>.md` |
| **記述内容** | 手順・guard・テンプレ | モデル指定・権限制約・出力契約・SubAgent description |
| **作業手順の所在** | SKILL.md 本体に書く | SubAgent は SKILL.md を参照する形を取る |

SubAgent 定義に作業手順を厚く書きすぎると skill とのコンテクスト重複が発生する。SubAgent は「どの skill のどの手順を、どのモデル・権限・出力契約で実行するか」を定義する場所とする。

### shared reference は references/ に置く

複数 skill / agent が共通参照するガイドライン（VC 作成・Anchor Verification 等）は、独立 skill にせず、最も主体的に使う skill の `references/<topic>.md` に配置する。他からは相対パスで参照する。

例: VC 作成ガイダンス は `.claude/skills/create-issue/references/body-authoring.md` に置き、`issue-author` SubAgent から相対パスで参照する。
