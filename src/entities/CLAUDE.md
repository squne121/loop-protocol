# src/entities — エンティティ型・コンポーネント

## 役割

ECS 風のエンティティ ID 型、Component 型、Unit 型を定義する。

## 不変条件

- 純粋な型定義 + 最小ファクトリ関数のみ
- `src/state`・`src/systems`・`src/render` への依存を作らない（逆向きは可）
- Entity ID は **数値型のみ**（参照ポインタ禁止 — ADR 0001）
- Component は純粋データ。ロジックを含めない

## 関連

- ルート `CLAUDE.md`
- `docs/adr/0001-architecture-baseline.md`
- `src/state/CLAUDE.md`
