# Agent / Skill 責務境界

LOOP_PROTOCOL の Issue 駆動開発で使う各 SubAgent / Skill の責務境界を、開発者が運用上参照するためのドキュメント。
SKILL.md / SubAgent 定義に書くとコンテクスト汚染になるため、本ドキュメントを正本とする。

## 基本モデル

```
SubAgent（役割）── Skill（作業手順）
        │              │
        │              └─ references/（補助ドキュメント）
        │
        └─ 必要な複数の Skill を **使う**
```

- **SubAgent = 役割**: 「何を担当する人物か」。隔離されたコンテクストで動く実行者
- **Skill = 作業手順**: 「どう作業するか」の再現可能な手順書
- **関係**: SubAgent が Skill を使う。SubAgent と Skill は責務分離するものではなく、役割と手順の関係

### アンチパターン

- SubAgent 定義に詳細な作業手順を埋める（手順は Skill 側に書く）
- 複数 Skill が共有する説明・概念を独立 Skill にする（references/ または本ドキュメントに置く）
- 「Why this SubAgent exists」のような普遍的説明を SubAgent 定義に書く（普遍は本ドキュメントに集約）

## Issue 管理系

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `issue-author` | Issue を **起票・修正** する役割 | `create-issue`（新規起票）、`edit-issue`（既存修正）|

| Skill | 手順 | 呼び出し元の例 |
|---|---|---|
| `create-issue` | 新規 Issue 起票の手順（Template Guard / Outcome Quality Guard / scope 重複チェック / `gh issue create`） | `issue-author` SubAgent、main session、`issue-refinement-loop`、`post-merge-cleanup` |
| `edit-issue` | 既存 Issue 本文更新の手順（バックアップ / Guard / 差分閾値 / `gh issue edit --body-file`）| `issue-author` SubAgent、`issue-refinement-loop`、`post-merge-cleanup`、`review-issue`（needs-fix 適用時） |
| `review-issue` | Issue 本文の品質を決定論的にチェックして verdict と差分提案を返す | main session、`issue-refinement-loop` |
| `issue-contract-review` | 実装着手直前に作業計画・コンテクスト・開発フロー適合性を preflight | main session、`implement-issue` の手前 |
| `issue-refinement-loop` | Issue 改善 4 段ループのオーケストレーター | main session |

共通参照: [`create-issue/references/body-authoring.md`](../../.claude/skills/create-issue/references/body-authoring.md)
（VC 作成ガイダンス・Anchor Verification・Machine-Readable Contract block guidance 等。`edit-issue` / `issue-author` も参照する）

## 実装系（C-2 で適合予定）

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `implementation-worker` | 実装作業の役割 | `implement-issue` |
| `test-runner` | Verification Commands 実行・AC 達成確認の役割 | （他 skill から委譲） |

| Skill | 手順 |
|---|---|
| `implement-issue` | 承認済み implementation issue を 1 PR で完了させる手順 |

## レビュー系（C-3 で適合予定）

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `pr-reviewer` | PR レビューの役割 | `pr-review-judge` |

| Skill | 手順 |
|---|---|
| `pr-review-judge` | PR の review verdict（APPROVE / REQUEST_CHANGES）を決定する手順 |

## オーケストレーション系（C-4 で適合予定）

| SubAgent | 役割 | 使う Skill |
|---|---|---|
| `post-merge-cleanup-worker` | PR マージ後 cleanup の役割 | `post-merge-cleanup` |

| Skill | 手順 |
|---|---|
| `impl-review-loop` | 実装→検証→PR レビュー の 4 段ループ手順 |
| `open-pr` | PR 起票手順 |
| `post-merge-cleanup` | PR マージ後の cleanup 手順 |

## 補助系

| SubAgent | 役割 |
|---|---|
| `codebase-investigator` | 大規模コードベース調査の役割 |

| Skill | 手順 |
|---|---|
| `ssot-discovery` | `docs/` 配下を SSOT として横断探索する手順 |
| `gemini-cli-headless-delegation` | Gemini CLI への headless 委譲手順 |
| `nlm-skill` | NotebookLM CLI / MCP 操作（既存導入） |

## 設計原則の補足

### review-issue と issue-contract-review の使い分け

| skill | 何を見るか | いつ呼ぶか |
|---|---|---|
| `review-issue` | Issue 本文の構造的品質（テンプレ準拠・AC 検証可能性・Outcome 具体性等） | Issue 起票後 / 改善ループ中 |
| `issue-contract-review` | 作業計画・コンテクストが指定通りで開発フローに沿って AI が安全着手できるか（VC preflight・AC 検証可能性・worktree/branch 命名） | 人間承認後・実装着手直前 |

責務が重なる項目（AC 検証可能性等）はあるが、**呼ぶタイミング**と **判定後の next action** が異なる。

### shared reference の置き場所

複数 skill が共通参照するガイドライン（VC 作成・Anchor Verification 等）は以下の順で配置を検討する:

1. 主体的に使う 1 つの Skill の `references/` 配下に置き、他 Skill から相対パスで参照
2. プロジェクト全体に関わる方針なら `docs/dev/` に置く
3. 独立 Skill にはしない（Skill は「何かを実行する手順」であり、共有参照は手順ではないため）

例: VC 作成ガイダンスは `create-issue/references/body-authoring.md` に置き、`edit-issue` / `issue-author` SubAgent から参照する。
