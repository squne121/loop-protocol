# scripts — プロジェクト補助スクリプト

## 役割

開発・運用・skill から呼ばれる補助スクリプト群（Python / Shell / Node）。

## 不変条件

- ライブラリ依存は最小化（外部依存ゼロを推奨。Python は stdlib のみ、Shell は POSIX 準拠が望ましい）
- 重い処理・破壊的処理は **dry-run モード** を実装する
- `gh` CLI などの外部コマンド呼び出しは `--repo` を明示してリポジトリ依存を減らす

## skill から呼ばれるスクリプトの規約

- skill 内から呼ぶスクリプトは `.claude/skills/<skill-name>/scripts/` 配下を優先
- リポジトリ全体で再利用するもののみ本ディレクトリに置く
- 構造化出力（YAML / JSON）で結果を返し、stdout を skill 側がパースできる形式とする

## 関連

- ルート `CLAUDE.md`
- `.claude/skills/*/scripts/` (skill 専用スクリプト)
