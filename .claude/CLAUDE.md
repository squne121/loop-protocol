# .claude — Claude Code 専用ローカル設定

## 役割

Claude Code が利用する設定・skill・agent 定義・一時領域。

## 構成

| サブディレクトリ | 内容 | Git 管理 |
|---|---|---|
| `.claude/skills/` | プロジェクト固有 skill 定義 | 管理（一部除く） |
| `.claude/agents/` | プロジェクト固有 SubAgent 定義 | 管理 |
| `.claude/rules/` | プロジェクト憲法の補足ルール | 管理 |
| `.claude/worktrees/` | git worktree の隔離配置先 | 除外（`.gitignore`） |
| `.claude/plans/` | SubAgent ハンドオフ用一時ファイル | 除外（`.gitignore`） |
| `.claude/settings.json` | Claude Code 共有設定 | 管理 |
| `.claude/settings.local.json` | Claude Code 個人設定 | 個人ごと（`.gitignore` 推奨） |

## skill / agent の不変条件

- skill / agent ファイルの編集は `skill-creator` スキルなど skill ガイドラインに従う
- 流用 skill のうち外部依存（CodexCLI 等）の整理は `docs/dev/imported-harness-triage.md` を参照
- skill 内のスクリプトは `.claude/skills/<name>/scripts/` 配下に置く（共有スクリプトは `scripts/` ルート）

## worktree の利用

- `git worktree add .claude/worktrees/<slug> -b worktree-<slug> main` で作成
- 配置先は **必ず** `.claude/worktrees/` 配下（リポジトリ外配置は禁止）
- マージ後は `git worktree remove` でクリーンアップ

## 関連

- ルート `CLAUDE.md`
- `docs/dev/workflow.md`
- `.claude/rules/project-constitution.md`
