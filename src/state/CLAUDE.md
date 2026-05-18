# src/state — 純粋データ層

## 役割

ゲーム状態を **純粋データ** として保持する層。シミュレーションの真実の状態。

## 不変条件

- すべて値型（プリミティブ / interface / 配列）。クラスや内包メソッドは作らない
- `src/render`・`src/ui`・DOM・Canvas API・`window`・`document` への参照を持たない
- 副作用なし。状態更新は `src/systems` 配下の System 関数経由のみ
- `src/data` の定数を import するのは可。ただし `src/data` から型のみを取り出す方が望ましい

## エクスポート規約

- 型定義は `index.ts` で再エクスポート
- ファクトリ関数（`createInitialGameState()` 等）は state 専用ファイルに置く

## 関連

- ルート `CLAUDE.md`（state ↔ render 完全分離）
- `docs/adr/0001-architecture-baseline.md`
- `src/systems/CLAUDE.md`（state を更新する側の規約）
- `src/render/CLAUDE.md`（state を読む側の規約）
