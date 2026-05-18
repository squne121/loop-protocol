# tests

## テスト戦略

- TDD: 実装前にテストを書く
- BDD（Behavior-Driven Development）: `it` / `test` の記述は `GIVEN <前提> WHEN <操作> THEN <期待結果>` 形式
- 実装詳細でなく **入出力の振る舞い** をアサーション

## カバレッジ方針

- 純粋ロジック（`src/state` / `src/systems` / `src/data`）は厚くテスト
- Canvas / DOM 描画はテスト対象外（E2E は別途）
- 境界値（0、最大値、空入力）と異常系を含める

## モック

- 内部モジュールは原則モックしない（実物で結合確認）
- 外部依存（`localStorage`、時刻取得等）のみモック化可
