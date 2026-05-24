# Directory Structure

`src/state`, `src/systems`, `src/render`, `src/input`, `src/data`, `src/entities`, `src/ui`, `src/storage` の責務境界を整理するための開発者向けメモ。

## Top Level

- `src/`: ゲーム本体のコード。
- `tests/`: Vitest による純ロジックと永続化境界の検証。
- `docs/`: 仕様と構成ルールの正本。
- `.github/`: build と unit test を通す最小 CI。
- `.devcontainer/`: WSL2 / container 共用の最小開発環境。
- `AGENTS.md`: Codex project-local の実行方針。
- `.codex/`: Codex project-local config / rules surface。
- `assets/`: 人手管理アセット。AI は直接編集しない。
- `LICENSES/`: ライセンス分離。

## Docs Roles

- `docs/product/requirements.md`: 全体要件と非ゴールの正本。
- `docs/product/features/<feature>.md`: 個別機能の stable な仕様置き場。
- `docs/product/game-overview.md`: 体験とループの概要説明。
- `docs/dev/current-focus.md`: 現在のフェーズと優先順位を示す一時メモ。
- `docs/adr/`: 設計判断の理由を残す場所。

## Runtime Layers

- `src/state/`: `GameState` と snapshot。描画や DOM に依存しない。
- `src/systems/`: 固定タイムステップで state を更新する純ロジック。
- `src/render/`: state を Canvas に描画する層。
- `src/input/`: DOM イベントを `InputCommand` に正規化する層。
- `src/ui/`: HUD とコマンド UI を DOM で組み立てる層。
- `src/storage/`: localStorage を隠蔽し、snapshot の保存/復元を担当。
- `src/data/`: ユニット、敵、武器などのデータ定義。
- `src/entities/`: ID とコンポーネントの基礎型。

## Dependency Rules

- `systems` は `state` と `input` を読むが、`render` と `ui` を呼ばない。
- `render` は `state` を読むだけで、状態変更を行わない。
- `ui` は `GameState` を直接書き換えず、コールバック経由で保存やリセットを依頼する。
- `storage` は snapshot 境界だけを扱い、ランタイムの描画や DOM には依存しない。

## Current MVP

- いまの MVP では `audio`、`campaign`、`network` は作らない。
- 先に戦闘キャンバス、HUD、保存、unit test の基礎を安定させる。
