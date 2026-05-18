# src/storage — 永続化境界

## 役割

`localStorage` 等への保存・読み出しを隠蔽する。snapshot を介した永続化境界。

## 不変条件

- 永続化先（localStorage / IndexedDB 等）の API 詳細を本ディレクトリで隠蔽
- 入出力は **snapshot 型**（純粋データ）のみ。`GameState` 全体を直接書き込まない
- `src/state` から snapshot 生成、`src/state` への snapshot 復元のみ
- エラーは型で表現（throw を避け、Result 型 / undefined / null 等で返す）

## テスト方針

- localStorage モック化または `globalThis.localStorage` のスタブ
- 保存→読み出しのラウンドトリップを必ずテスト

## 関連

- ルート `CLAUDE.md`
- `docs/adr/0001-architecture-baseline.md`（storage は snapshot を介した永続化境界）
- `src/state/CLAUDE.md`
- `tests/storage.test.ts`
