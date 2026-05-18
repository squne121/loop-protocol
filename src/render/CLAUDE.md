# src/render — 描画専用層

## 役割

Canvas API による描画 **だけ** を担う。`requestAnimationFrame` ループで呼ばれる。

## 不変条件

- `src/state` を **読み取り専用** で参照する。state を直接変更しない（state mutation は systems の責務）
- `src/systems`・`src/input`・`src/storage` への参照を持たない
- ゲームロジック（移動・当たり判定・スコア計算等）を書かない
- 描画上の派生状態（カメラ補間・パーティクル寿命等）は render 内部に閉じる

## Canvas / DOM 利用

- `Canvas API` の使用はこのディレクトリだけに許可される
- DOM 直接操作は `src/ui` の責務。ここでは触らない

## エクスポート規約

- 描画エントリは `render(state, ctx, dt)` 形式で統一
- 内部ヘルパは default export せず named export

## 関連

- ルート `CLAUDE.md`（state/render 分離・requestAnimationFrame）
- `docs/adr/0001-architecture-baseline.md`
- `src/state/CLAUDE.md`
- `src/ui/CLAUDE.md`
