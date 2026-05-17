# LOOP_PROTOCOL Claude Code 運用入口

この文書は、`LOOP_PROTOCOL` で AI が最初に読む短い入口です。
詳細な実装手順や docs の更新規則は `.claude/rules/project-constitution.md` を参照してください。

## まず読む順序

1. 対象の GitHub Issue 本文と、必要ならコメント
2. この `CLAUDE.md`
3. `.claude/rules/project-constitution.md`
4. `docs/product/requirements.md`
5. `docs/dev/current-focus.md`
6. 関連する `docs/adr/*.md`、`docs/product/features/<feature>.md`、`docs/dev/directory-structure.md`

## 不変のアーキテクチャ原則

- `src/state` と `src/render` は完全に分離する。
- `src/systems` の更新ロジックは DOM や Canvas API に依存しない。
- 描画は `requestAnimationFrame`、シミュレーションは固定タイムステップ 60Hz のアキュムレータで進める。
- UI は DOM、戦闘表示は Canvas として分離する。
- 武器、敵、ユニットなどのデータ定義は `src/data` に寄せる。
- 永続化は `src/storage` の snapshot 境界を通す。

## 絶対非ゴール

- 既存作品の固有名詞、画像、音声、キャラクター、テキストの流用
- 既存作品の直接再現
- Issue や spec にない campaign / territory / network / multiplayer の追加
- 高品質アセット前提の演出を先に作ること
- `systems` 層から描画や DOM を直接触ること

## 保護領域

- `assets/` と `LICENSES/` は人手管理とし、明示指示がない限り直接編集しない。
- ライセンス判断が必要な変更は人間判断へ戻す。

## 現在の開発フェーズ

- 現在は `M1: Foundation Gate (v0.1.x)` を進行中。
- まずは開発基盤、運用ルール、最小仕様正本を固める。
- 詳細な優先順位は `docs/dev/current-focus.md` を参照する。

## 最低限の確認コマンド

- `pnpm typecheck`
- `pnpm lint`
- `pnpm test`
- `pnpm build`

## 詳細ルールの置き場

- 実装手順と検証: `.claude/rules/project-constitution.md`
- 全体要件と非ゴール: `docs/product/requirements.md`
- 現在の優先順位: `docs/dev/current-focus.md`
- 構成ルール: `docs/dev/directory-structure.md`
- アーキテクチャ理由: `docs/adr/0001-architecture-baseline.md`
