# Issue Refinement Loop Sidecar

`issue-refinement-loop/SKILL.md` から退避した GitHub issue 更新、Ledger、decision-only 判定、handoff 更新の長文規約を集約する。

## Body File Guidance

- Issue 本文更新時にコードブロック内で `\` 行継続を使うなら、repo 配下 `tmp/` の一時ファイルを使い、`gh issue edit --body-file` を優先する
- `gh issue edit --body-file` 直前に `wc -c "$BODY_FILE"` で空ファイルや 1 byte ファイルを弾く
- `grep -Pq '\\\\(?:\"|\\$)' "$BODY_FILE"` で `\\\"` / `\\$` の混入を確認し、HEREDOC 由来か文字列リテラルかを見極める
- HEREDOC サンプルにコードフェンスを含める場合は ````` `` ではなく `~~~yaml` / `~~~` を使う

## Parent Mode Classification

- `delivery-rollup`: child implementation / follow-up の完了を rollup して close する parent
- `quality-gate`: child 完了と quality decision を分離し、Quality Decision Record の確定まで親を close しない parent
- `routing-map`: canonical destination / desired destination の地図を維持する parent
- `decision-log`: 意思決定記録と next action の固定を主目的にする parent

`closure_mode` は `child-complete` / `measurement-ready` / `quality-validated` / `routing-complete` / `decision-recorded` の enum 固定とし、互換は `delivery-rollup -> child-complete`、`quality-gate -> measurement-ready | quality-validated`、`routing-map -> routing-complete`、`decision-log -> decision-recorded` に限定する。

fail-close 条件:

- `parent_mode` が欠落している parent issue は自動推定で確定しない。更新案の提案まではよいが、actual update は停止する
- `closure_mode` と `Quality Decision Record.Status` が不一致なら invalid として close 判定へ進まない
- `<required: ...>` placeholder や enum 外値は missing 扱いにし、close-ready とみなさない
- runtime guard が未接続の `quality-gate` parent は body 更新だけで auto-close-ready に進めない

## Decision-only Issue Rules

`decision-only` は最終実装をしない issue として扱う。以下を必須とする。

- `state/needs-human` を含む、または `decision-only` / `意思決定 issue` の明示が `Outcome` / `In Scope` / `Out of Scope` / `Handoff Contract` にある
- `## Next Action` が 1 つの実行可能アクションとして明確
- `## Handoff Contract` が `記録先` / `参照先` / `次接続先` の 3 点を満たす

不足がある場合は `needs-fix` で再処理し、実装フェーズへ誤判定しない。

## Human Intent Ledger

抽出対象:

- Human-stated desired outcome
- anti-goals
- required references
- suspected misreadings
- HIGH gap
- desired destination
- current validated scope
- destination routing status

最小テンプレート:

```text
## Human Intent Ledger（iteration N）

- Human-stated desired outcome: ...
- anti-goals: ...
- required references: ...
- suspected misreadings: ...
- HIGH gap: ...
- desired destination: ...
- current validated scope: ...
- destination routing status: open issue | existing issue | unresolved
```

判定ルール:

- 人間コメント内のコードブロックは否定シグナル優先で解釈する
- 肯定参照シグナル（例: 「以下の通り」「下記を採用」）がある場合のみコードブロックを仕様として採用する
- 否定と肯定が混在した場合は否定を優先する

停止シグナル:

- 根本変更: 目的の再定義
- 訂正: 「それではない」「違う」「誤り」
- スコープ上書き: Outcome の書き換え
- 明示的停止: 「やり直して」「この方向ではない」

停止シグナルを検出した場合の fail-close:

1. `## スコープ変更シグナル検出` コメントを Issue に記録する
2. Outcome 変更内容、既存レビュー無効化の可否、再開コマンドを人間へ明示する
3. review / delegation / `issue-author` に進まず、その iteration を即停止する

最小コメント要素:

```text
## スコープ変更シグナル検出: 前提確認フェーズで停止

- 検出したシグナル
- コメント投稿者
- コメント日時
- 人間への確認事項
```

## Iteration >= 1 Ledger Refresh

1. 直前の `LOOP_STATE` コメント時刻を取得する
2. それ以降の新規コメントを収集する
3. 人間投稿だけを対象に Ledger を差分更新する
4. `ledger_updated` と変更要約を次の `LOOP_STATE` に記録する
5. 停止シグナルが見つかった場合は、iteration 0 と同じ fail-close 手順で停止する

## GitHub Ops Logging And Fallback

SubAgent 出力は Issue コメントへ記録する。基本フォーマット:

```text
## <フェーズ名>（iteration <N>）

**実行SubAgent**: <subagent_name>
**判定**: <approve / needs-fix / needs-attention / N/A>

<SubAgent の出力全文>

---
*by <SubAgent名>, <ISO8601 UTC タイムスタンプ>*
```

エラー時:

1. エラー内容とフェーズを記録する
2. 同一コマンドを最大 3 回再試行する
3. 失敗継続時は main conversation へ手動実行用コマンドを返す
4. コメント失敗は継続可、本文更新失敗は人間承認後に続行する
5. ただし `invoked_as_loop: true` の自動承認フローで、本文更新前に必須の監査コメントを残せなかった場合は fail-close で停止し、`gh issue edit` を実行してはならない
6. `## スコープ変更シグナル検出` は mandatory audit comment として扱い、投稿失敗時は手動実行用コマンドを返して hard stop する

## Handoff Artifact Update

handoff 正本は 1 件の URL に固定し、iteration ごとに `supersedes` で上書き関係を保持する。

```text
handoff_artifact: <更新通知コメント URL>
supersedes: <前 iteration の handoff_artifact | none>
```
