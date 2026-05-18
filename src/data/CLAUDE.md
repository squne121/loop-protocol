# src/data

## 不変条件

- ロジック（if / while / 関数本体）を含まない。純粋なデータ列挙のみ
- 1 ファイル 1 ドメイン（`enemies.ts` / `weapons.ts` / `units.ts` 等）
- バランス調整は数値変更のみ。型変更は import 元（systems 等）への影響が大きいため別 Issue
