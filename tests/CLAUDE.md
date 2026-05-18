# tests — Vitest テスト層

## テスト戦略

- **TDD（テスト駆動開発）**: 実装前にテストを書く
- **BDD（振る舞い駆動開発 = Behavior-Driven Development）**: テスト名と記述は GIVEN/WHEN/THEN 命名規則
- 実装詳細でなく **入出力の振る舞い** をアサーションする

## 命名規則

- ファイル名: `<対象>.test.ts`（例: `movement-system.test.ts`）
- describe ブロック: 対象モジュール名
- it / test ブロック: `GIVEN <前提条件> WHEN <操作> THEN <期待結果>`

## カバレッジ方針

- 純粋ロジック（`src/state`・`src/systems`・`src/data`）は **厚く** テストする
- `src/render`・`src/ui` の Canvas / DOM 描画はテスト対象外（E2E は別途検討）
- 境界値（0、最大値、空入力）と異常系を含める

## モック方針

- 内部モジュールはなるべくモック化しない（実物で結合確認）
- 外部依存（`localStorage`、時刻取得等）のみモック化を許可
- モック化したものは必ずテスト内で明記

## 関連

- ルート `CLAUDE.md`
- `src/systems/CLAUDE.md`（System 関数のテスト規約）
- Vitest 設定: `package.json` の `"test": "vitest run"`
