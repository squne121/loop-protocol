# src/ui — DOM UI 層

## 役割

HUD・メニュー・モーダル等の DOM ベース UI を担当。Canvas 内 UI とは混在させない。

## 不変条件

- DOM 操作は本ディレクトリと `src/input` のみに許可
- `src/state` を **読み取り専用** で参照（必要なら snapshot 経由が望ましい）
- ゲームロジック（state mutation）を書かない
- Canvas 描画は `src/render` の責務

## 関連

- ルート `CLAUDE.md`
- `docs/adr/0001-architecture-baseline.md`（ui は DOM、Canvas 内 UI と混在禁止）
- `src/render/CLAUDE.md`
