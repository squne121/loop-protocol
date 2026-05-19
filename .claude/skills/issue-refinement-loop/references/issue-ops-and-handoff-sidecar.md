# Issue Refinement Loop Sidecar

`issue-refinement-loop/SKILL.md` から退避した GitHub Issue 更新 / Parent Mode / GitHub Ops Logging の補助規約を集約する。

## Body File Guidance

Issue 本文更新は `edit-issue` skill に委譲する前提だが、ループ中の orchestrator が直接コメント投稿する際は以下を守る:

- 長文・多行のコメントは repo 配下 `tmp/` の一時ファイル経由 (`gh issue comment --body-file`) を使う
- `--body-file` 直前に `wc -c "$BODY_FILE"` で空ファイル / 1 byte ファイルを弾く
- HEREDOC サンプルにコードフェンスを含める場合は ``` ではなく `~~~` を使う

## Parent Mode Classification

parent Issue のループ対象判定時に参照する分類:

- `delivery-rollup`: child implementation の完了を rollup して close する
- `quality-gate`: child 完了と quality decision を分離し、Quality Decision Record の確定まで close しない
- `routing-map`: canonical destination の地図を維持する
- `decision-log`: 意思決定記録と next action の固定を主目的にする

`closure_mode` は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` の closed enum。互換は `delivery-rollup → child-complete` / `quality-gate → measurement-ready | quality-validated` / `routing-map → routing-complete` / `decision-log → decision-recorded`。

### Fail-close 条件

- `parent_mode` 欠落 → 自動推定で確定しない（提案までで止める）
- `closure_mode` と `Quality Decision Record.Status` 不一致 → invalid、close 判定不可
- `<required: ...>` placeholder や enum 外値 → missing 扱い

## GitHub Ops Logging

ループ中の orchestrator は以下を Issue コメントに記録する:

- 各イテレーション開始時に `## issue-refinement-loop: iteration <N> 開始` のヘッダ
- Step 2 の `REVIEW_ISSUE_RESULT_V1` 要約
- Step 4 の `ISSUE_EDIT_RESULT_V1` 要約
- 終了時に `## issue-refinement-loop: 完了` のヘッダ + LOOP_STATE 最終値

```bash
gh issue comment <issue_number> --body "## issue-refinement-loop: iteration <N> 完了
- verdict: <...>
- improvements_applied: <...>
- 次イテレーション: <Yes / No (理由)>"
```

## Scope 変更シグナル検出

Issue 本文に以下が新規追加された場合は、refinement のスコープ拡大兆候として **次イテレーションに進まず停止**:

- `## In Scope` に新規の機能領域追加
- `## Allowed Paths` に新規のディレクトリ追加（特に既存と異なるアーキテクチャ層）
- `## Acceptance Criteria` に新規の検証可能性が低い項目追加

停止時は人間判断（`termination_reason: human_escalation`）を仰ぐ。
