# src/ui

## 不変条件

- DOM 操作は本ディレクトリと `src/input` のみに許可
- `src/state` は **読み取り専用** で参照（必要なら snapshot 経由が望ましい）
- ゲームロジック（state mutation）は書かない（systems の責務）
- Canvas 描画は `src/render` の責務（ui では触らない）
