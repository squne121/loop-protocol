# Follow-up Materialization

## Step 4.5 gate

`approve` の直後は Step 5 に進む前に child / follow-up materialization gate を通す。

- delivery-rollup parent で child slot が未 materialize の場合は、main thread が routing 先を確認してから `issue-author` に委譲する
- 通常の implementation / refinement issue では gate を通過してそのまま Step 5 へ進む

## Derived follow-up issues

scope 外だが記録価値のある改善候補を見つけた場合は、別 Issue として分離する。

- dedupe は `FOLLOW_UP_ISSUE_REQUEST_V1.dedupe_key` を検索キーにする
- open の重複がある場合は再利用し、新規起票しない
- closed issue の reopen は自動実行しない
- 起票またはスキップ結果は終了報告に列挙する

## Must not

- title の類似検索だけで dedupe を済ませない
- 本体 Issue の scope に押し込んで `1 Issue = 1 PR` を崩さない
