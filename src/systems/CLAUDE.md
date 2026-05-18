# src/systems — 更新ロジック層

## 役割

固定タイムステップ 60Hz のアキュムレータループで呼ばれる **純粋ロジック**。state を更新する。

## 不変条件（CLAUDE.md ルートの「憲法」）

- DOM・Canvas API・`window`・`document`・`navigator` への参照を **一切持たない**
- `src/render`・`src/ui` への参照を持たない
- 入力は `src/input` の `InputState` / `InputCommand` 経由
- データ定数は `src/data` 配下から import する

## System 関数の規約

- シグネチャ: `run<Name>System(state: GameState, commands: InputCommand[], deltaMs: number): void`
- state を直接書き換えてよい（mutation）が、副作用は state に閉じる
- 例外を投げない（state を不整合にしないため）

## テスト方針

- すべての System 関数に対応するテストを `tests/<name>-system.test.ts` に置く
- BDD（Behavior-Driven Development）形式で GIVEN/WHEN/THEN の振る舞いを記述
- 境界値（クールダウン 0ms、入力なし、最大値到達）を含めること

## 関連

- ルート `CLAUDE.md`（systems は DOM/Canvas 非依存・固定タイムステップ）
- `docs/adr/0001-architecture-baseline.md`
- `src/state/CLAUDE.md`
- `src/input/CLAUDE.md`
- `tests/CLAUDE.md`
