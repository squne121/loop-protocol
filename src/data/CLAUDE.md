# src/data — データ定義層

## 役割

武器・敵・自機ユニット等のパラメータを **外部定義** として集約する。コードからは const として import される。

## 不変条件

- ロジック（if/while/関数）を含まない。純粋なデータ列挙のみ
- `src/state`・`src/systems`・`src/render` への参照を持たない
- 型は `src/entities` または `src/data` 内で定義
- 1 ファイル 1 ドメイン（`enemies.ts` / `weapons.ts` / `units.ts` 等）

## データ追加・改修

- 既存項目の追加・修正は本ディレクトリ内で完結させる
- 型定義の変更は import 元（`src/systems` 等）への影響が大きいため、別 Issue で扱う
- バランス調整は数値のみ変更（構造変更は慎重に）

## 関連

- ルート `CLAUDE.md`（データは src/data 配下で管理）
- `src/data/README.md`（あれば優先）
- `src/entities/CLAUDE.md`
