---
name: issue-driven-dev
description: GitHub Issue を実装契約として扱い、worktree 隔離・実装ディスパッチ・サブエージェントへの監査委譲を順番に行うルーティングスキル。「#数字」「Issue を実装」「Issue 駆動」「PR化」「GitHub Issue を実装」「implement issue」などの依頼で必ず使用すること。実装計画の自然言語生成はしない（Plan モード廃棄済み）。
---

# issue-driven-dev — 決定論的ルーティング

このスキルは **AI に考えさせるための手順書ではなく、決定論的なシステム（スクリプト / git CLI / サブエージェント）を正しい順番で呼び出すルーティング定義** である。

各フェーズは IF 分岐で実行可否を判定し、失敗時は人間へ差し戻す。skill 内に自然言語の作業手順を書かない。

## 前提（重複記述の禁止）

- プロジェクト憲法は `CLAUDE.md` が単一の正本（自動ロード済み）。本 skill 内で再説明しない。
- ワークフロー全体の SSOT は `docs/dev/workflow.md`。本 skill 内で再説明しない。
- 検証（typecheck / lint / test / build）は Claude Hooks / Git Hooks / CI が担う。本 skill 内で呼び出さない。

## フェーズ 1: ガードレール検知

```bash
scripts/check-issue-contract.sh <ISSUE_NUMBER>
```

| 終了コード | 意味 | 次アクション |
|---|---|---|
| 0 | 必須項目を満たす | フェーズ 2 へ |
| 2 | 必須項目欠落 | **作業中止**。人間に Issue Forms 準拠での追記を要求 |
| 1 | スクリプト実行エラー | 中止。原因を人間へ報告 |

`scripts/check-issue-contract.sh` の判定基準は `.github/ISSUE_TEMPLATE/implementation.yml` と一致させる。skill 側で「何が必須か」を再定義しない。

## フェーズ 2: 環境隔離

```bash
git worktree add .claude/worktrees/issue-<ISSUE_NUMBER>-<slug> -b worktree-issue-<ISSUE_NUMBER>-<slug> main
cd .claude/worktrees/issue-<ISSUE_NUMBER>-<slug>
```

- 配置先は **必ず** `.claude/worktrees/` 配下（`.gitignore` 除外済み）
- `permissions.additionalDirectories` は workspace trust prompt をスキップしないため、外部配置は禁止
- `git worktree add` の CLI を直接使う（Claude Code 専用機能 `--worktree` には依存しない）
- 既に worktree 内にいる場合はこのフェーズをスキップ

## フェーズ 3: 実装ディスパッチ

- Plan モード不使用。Issue を実装契約として直接 Edit する。
- TDD：**先に Vitest テストを書く**（`tests/` 配下）。テストが落ちることを確認してから実装する。
- BDD：テスト記述は GIVEN/WHEN/THEN の命名規則を使う（Behavior-Driven Development。プロパティテスト駆動開発ではない）。
- スコープ：Issue「変更許可領域」セクションに列挙されたパス以外を編集しない。
- 検証コマンドは自分で実行しない（Claude Hooks `PostToolUse` / Git Hooks `pre-commit` が自動実行する）。

## フェーズ 4: サブエージェント委譲

実装とローカルテストが完了したら、本 skill のスコープは終了する。
**メインセッション自身で `gh pr create` を実行してはならない**（Context Rot による幻覚要因）。

```
# 1. ハンドオフファイルを書き出す
.claude/plans/issue-<ISSUE_NUMBER>-handoff.md
  - 対象 Issue 番号
  - worktree のパスとブランチ名
  - 変更対象ファイル一覧
  - 受け入れ条件達成状況の自己申告

# 2. architecture-reviewer サブエージェントへ Task で委譲
Task tool:
  subagent_type: architecture-reviewer
  prompt: ".claude/plans/issue-<ISSUE_NUMBER>-handoff.md を読み、監査・PR 起票せよ"
```

`architecture-reviewer` の判定：

| 結果 | 次アクション |
|---|---|
| 合格 → PR 起票完了 | メインセッション終了。worktree は keep（PR マージ後に手動 remove） |
| REJECTED → 不適合返却 | メインセッションが不適合を解消 → 再度フェーズ 4 を実行 |

## やってはいけないこと（決定論的禁則）

| 禁則 | 理由 |
|---|---|
| 「実装計画書」を skill 内で自然言語生成する | Plan モード廃棄済み運用と矛盾 |
| `pnpm typecheck` 等を skill 内で書く | Hooks/CI の責務と混在し責務分離が崩れる |
| `gh pr create` をメインセッションが直接実行する | Context Rot による幻覚要因。`architecture-reviewer` の責務 |
| worktree をリポジトリ外に作成する | workspace trust prompt が再発し承認マシーン化する |
| `git commit --no-verify` / `git push --no-verify` を使う | Git Hooks の履歴保護をすり抜ける |
| 変更許可領域外のファイルを編集する | スコープ肥大化を防ぐ |

## 関連リソース

- `scripts/check-issue-contract.sh` — フェーズ 1 のガードレール検知本体
- `.github/ISSUE_TEMPLATE/implementation.yml` — Issue Forms（フェーズ 1 の判定対象）
- `.claude/agents/architecture-reviewer.md` — フェーズ 4 の委譲先サブエージェント
- `docs/dev/workflow.md` — SSOT。本 skill は SSOT の AI 向け実装層
- `CLAUDE.md` — プロジェクト憲法（自動ロード）
