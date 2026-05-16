# LOOP_PROTOCOL Claude Code 運用ルール

## プロジェクトの憲法

- `src/state` と `src/render` は完全に分離すること。
- `src/systems` の更新ロジックは、DOM や Canvas API に一切依存してはならない。
- 描画は `requestAnimationFrame` で行い、シミュレーションは固定タイムステップ 60Hz のアキュムレータで進めること。
- 武器・敵パラメータ等のデータ定義は `src/data` 配下の外部ファイルで管理すること。
- `assets/` や `LICENSES/` ディレクトリ配下はライセンス管理の都合上、人間が手動で管理するため、明示的な指示がない限り AI は直接編集しないこと。
