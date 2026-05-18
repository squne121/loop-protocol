# Rule: git-policy

Git 操作の不変条件。

## 1. 1 Issue = 1 PR

- 1 つの Issue に対して必ず 1 つの PR を作成する
- 実装中に別の問題を発見した場合は新規 Issue を起票し、現 Issue のスコープを保つ
- 複数 Issue を 1 PR にまとめることは原則禁止

## 2. Worktree 配置

- 配置先: `.claude/worktrees/<issue-slug>/`（リポジトリ内）
- 配置先は `.gitignore` で除外済み
- `git worktree add` の CLI を直接利用（特定エージェント専用機能には依存しない）
- 例: `git worktree add .claude/worktrees/issue-42-foo -b worktree-issue-42-foo main`

リポジトリ外への配置は禁止（Claude Code の workspace trust prompt が再発し、承認マシーン化するため）。

## 3. ブランチ命名

- `worktree-<short-slug>` または `worktree-issue-<番号>-<slug>` を推奨
- main へ直接コミットしない
- force push は main / 共有ブランチに対して禁止

## 4. コミットメッセージ

- Conventional Commits 風: `<type>: <subject> (#<issue>)`
- type 例: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`
- 本文に変更理由と影響範囲を簡潔に記載
- Co-Authored-By トレーラーを使用（複数人/AI の協働時）

## 5. `--no-verify` / hook スキップ禁止

- `git commit --no-verify` 禁止
- `git push --no-verify` 禁止
- `git config core.hooksPath` の変更禁止
- Git Hooks をすり抜けることは履歴保護層を無効化する行為と見なす

## 6. マージ後クリーンアップ

PR マージ後は以下を実行：
```bash
git worktree remove .claude/worktrees/<slug>
git branch -d worktree-<slug>
```

詳細は `post-merge-cleanup` skill を参照。
