# src/systems

## 不変条件

- DOM / Canvas API / `window` / `document` / `navigator` への参照を **一切持たない**
- 入力は `src/input` の `InputState` / `InputCommand` 経由でのみ受ける
- データ定数は `src/data` 配下から import する

## System 関数の規約

- シグネチャ: `run<Name>System(state: GameState, commands: InputCommand[], deltaMs: number): void`
- state mutation 許可。例外は投げない（state を不整合にしないため）

## テスト

- 対応する Vitest を `tests/<name>-system.test.ts` に置く
- GIVEN/WHEN/THEN 命名、境界値（クールダウン 0、入力なし、最大値到達）を含める
