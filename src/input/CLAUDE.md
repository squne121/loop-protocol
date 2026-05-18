# src/input — 入力マッピング層

## 役割

ブラウザ入力（keyboard / pointer）を **抽象化された InputCommand** に変換する。

## 不変条件

- DOM イベントハンドラの登録は本ディレクトリで完結
- `src/state`・`src/render`・`src/ui` に依存しない
- 出力は `InputCommand`（move / aim / fire 等の判別共用体）として `src/systems` へ渡す

## InputCommand の追加

- 判別共用体の `type` を新規追加する場合は、対応する System 側のハンドラも同時更新が必要
- 互換性破壊を伴う場合は別 Issue で段階的に進める

## 関連

- ルート `CLAUDE.md`
- `src/systems/CLAUDE.md`（InputCommand を受け取る側）
