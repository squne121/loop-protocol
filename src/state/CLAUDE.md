# src/state

## 不変条件

- すべて値型（プリミティブ / interface / 配列）。クラスや内包メソッドは作らない
- `src/render` / `src/ui` / DOM / Canvas API / `window` / `document` への参照を持たない
- 副作用なし。state mutation は `src/systems` の System 関数経由のみ

## エクスポート規約

- 型定義は `index.ts` から再エクスポート
