# scripts

## 不変条件

- 外部ライブラリ依存はゼロを目標とする（Python は stdlib のみ、Shell は POSIX 準拠）
- 破壊的処理は **dry-run モード** を必ず実装する
- skill 専用スクリプトは `.claude/skills/<name>/scripts/` 配下に置き、本ディレクトリは複数 skill で共有するものに限る
- 構造化出力（YAML / JSON）で結果を返し、skill 側がパース可能な形式とする
