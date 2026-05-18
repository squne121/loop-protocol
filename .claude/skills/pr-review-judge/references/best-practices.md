# Best Practices

## Purpose

`pr-review-judge` は linked issue の contract と PR evidence を判定し、GitHub surface へ verdict を残す。共有運用のガバナンスは `.agents/skills/shared-agent-skills-governance/` に委譲する。

## Practices

- self-authored PR では `gh pr review --comment` を使い、APPROVE / REQUEST_CHANGES 共に `PullRequestReview` オブジェクトとして review 履歴に残す。`--approve` / `--request-changes` は試行しない。
- 他者の PR をレビューする場合は `gh pr review --approve` / `--request-changes` を使う。
- canonical result は GitHub surface に置き、stdout は実行ログと要約に留める。
- comment には `Evidence Check` を残し、次の reviewer が再現できる形にする。
- CI が `pending` / `in_progress` の場合は `timeout 3600 gh pr checks <PR番号> --watch` で完了を待つ。
  待機中は linked issue 照合・diff・AC coverage 等の非 CI 観点レビューを先行して完了させる。
  CI 完了後に CI 結果を統合して最終 verdict を出す。CI fail は常に blocker とする。
- `gh pr checks --watch` にはネイティブのタイムアウトオプションがないため、シェルレベルの
  `timeout <秒数>` と組み合わせて使う（例: `timeout 3600 gh pr checks <PR番号> --watch`）。
  タイムアウトは CI が異常に長引いた場合の安全弁であり、正常終了時は `--watch` が CI 完了を検出した時点で抜ける。
  このパターンは CI 待機フローを他スキルへ横展開する際にも共通して適用する。
- domain-specific な検証ステップを追加する場合は `Step 3x` パターンで拡張する:
  - 発動条件: `gh pr diff <PR番号> --name-only | grep "<対象ディレクトリ>"` で実 diff を確認する。PR body の `Changed Paths` は参考情報に留め、正とはしない。
  - チェック内容: 対象の tasks.md / spec に記載された実機検証要件の確認 + evidence レポートの存在と pass 判定。
  - 出典: PR #393（Issue #390）で `Kindle for PC2 実機検証` 要件の live 検証 evidence チェックとして確立。
- diff だけでは変更影響が判断しにくい場合は、PR ブランチの worktree を `tmp/pr-review-<PR番号>` に
  作成してローカル参照する（SKILL.md 内 Step 2a）。
  - **fetch**: `git fetch origin refs/pull/<PR番号>/head:refs/pr-review/<PR番号>` を使う。
    fork PR でも動作し、並行レビュー時の `FETCH_HEAD` 上書き問題を回避できる。
    （`git fetch origin <branch_name>` は fork PR で失敗するため使わない）
  - **worktree 作成**: `git worktree add --detach tmp/pr-review-<PR番号> refs/pr-review/<PR番号>`
  - **禁止事項**: worktree 内でのコミット・push は禁止。`--detach` は技術的な書き込みロックではなく、
    運用上の約束として守ること。
  - **クリーンアップ**: `git worktree remove --force` + `git update-ref -d refs/pr-review/<PR番号>`。
    `--force` により検証生成物が残っていても削除できる。
  - **事前削除**: 実行開始時に `git worktree remove --force ... 2>/dev/null || true` で前回の残存を回収する。
    前回中断した場合でも次回実行時の事前削除で自動回収される。
  - **省略条件**: 変更ファイル数 ≤ 3 かつ追加/削除行数 ≤ 50 行で影響範囲が自明な場合はスキップしてよい。
  - worktree 内では SerenaMCP のシンボル分析（`find_referencing_symbols` 等）や test/lint の実行が可能。
  出典: Issue #449 / PR #455。
- skill 手順書でファイルパスを判定する際は、人間が書く自由記述（PR body `Changed Paths` 等）ではなく、
  `gh pr diff --name-only` の実 diff を正とする。記載漏れ・記載ミスによる false negative を防ぐため。
  出典: PR #393 レビュー non-blocker 対応。
