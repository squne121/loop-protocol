# src/input

## 不変条件

- DOM イベントハンドラ登録は本ディレクトリで完結
- 出力は `InputCommand`（move / aim / fire 等の判別共用体）として `src/systems` へ渡す
- `src/state` / `src/render` / `src/ui` に依存しない

## InputCommand の追加

- 判別共用体の `type` を新規追加する場合は対応する System 側ハンドラを同時更新
- 互換性破壊を伴う場合は別 Issue
