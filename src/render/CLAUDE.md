# src/render

## 不変条件

- `src/state` を **読み取り専用** で参照する（state mutation は systems の責務）
- ゲームロジック（移動・当たり判定・スコア計算等）を書かない
- 描画上の派生状態（カメラ補間・パーティクル寿命等）は render 内部に閉じる

## Canvas / DOM

- Canvas API の使用は本ディレクトリだけに許可
- DOM 直接操作は `src/ui` の責務（render では触らない）
