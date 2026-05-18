# src/storage

## 不変条件

- 永続化先（localStorage / IndexedDB 等）の API 詳細を本ディレクトリで隠蔽
- 入出力は **snapshot 型**（純粋データ）のみ。`GameState` 全体を直接書き込まない
- エラーは型で表現（throw を避け、Result 型 / undefined / null 等で返す）

## テスト

- localStorage のスタブ化 + 保存 → 読み出しのラウンドトリップを必ずテスト
