# src/entities

## 不変条件

- 純粋な型定義 + 最小ファクトリ関数のみ
- Entity ID は **数値型のみ**（参照ポインタ禁止 — ADR 0001）
- Component は純粋データ。ロジックを含めない
