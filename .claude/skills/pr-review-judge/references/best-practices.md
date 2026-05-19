# PR Review Best Practices

`pr-review-judge` skill の Procedure を補完する一般的な PR レビューのベストプラクティス。

## 待機と CI

CI が `pending` / `in_progress` の場合は完了を待つ。`gh pr checks --watch` にはネイティブのタイムアウトオプションがないため、シェルレベルの `timeout` を併用する:

```bash
timeout 3600 gh pr checks <PR番号> --watch
```

待機中は CI 完了を待たずに以下の非 CI 観点レビューを先行して完了させる:
- linked issue contract 照合
- diff 確認
- AC coverage
- Allowed Paths 整合

CI 完了後に CI 結果を統合して最終 verdict を出す。**CI fail は常に blocker**。

## ファイルパス判定の正

skill 手順書でファイルパスを判定する際は、**人間が書く自由記述（PR body の `Changed Paths` 等）ではなく、`gh pr diff --name-only` の実 diff を正** とする。記載漏れ・記載ミスによる false negative を防ぐため。

## PR ブランチの worktree 作成によるローカル参照（任意）

diff だけでは変更影響が判断しにくい場合、PR ブランチの worktree を作成してローカル参照する。

### 配置先

LOOP_PROTOCOL では worktree を必ず `.claude/worktrees/` 配下に作る（リポジトリ外配置は workspace trust prompt を再発させるため禁止）。

```bash
WORKTREE=".claude/worktrees/pr-review-<PR番号>"
```

### 手順

1. 事前削除（前回中断時の残存を自動回収）:
   ```bash
   git worktree remove --force "$WORKTREE" 2>/dev/null || true
   ```

2. PR ブランチを fetch して named ref に保存（fork PR でも動作、並行レビュー時の `FETCH_HEAD` 上書き回避）:
   ```bash
   git fetch origin refs/pull/<PR番号>/head:refs/pr-review/<PR番号>
   git worktree add --detach "$WORKTREE" refs/pr-review/<PR番号>
   ```

3. worktree 内でファイル参照・検証コマンドを実行する

4. レビュー完了後にクリーンアップ:
   ```bash
   git worktree remove --force "$WORKTREE"
   git update-ref -d refs/pr-review/<PR番号>
   ```

### 制約

- worktree 内でのコミット・push は禁止（`--detach` は技術的ロックではなく運用上の約束）
- `git fetch origin <branch_name>` は fork PR で失敗するため使わない（必ず `refs/pull/<PR番号>/head` を使う）

### 省略条件

変更ファイル数 ≤ 3 かつ追加 / 削除行数 ≤ 50 行で影響範囲が自明な場合はスキップしてよい。

## Verdict コメントの可読性

- `Evidence Check` セクションを必ず残し、次の reviewer が再現できる形にする
- LOOP_VERDICT YAML はコードフェンス（` ``` `）で囲む（heredoc 内でも `\` でエスケープしない）
- `reviewed_head_sha` は YAML ブロック内に 1 回だけ記載する
