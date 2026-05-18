# Rule: file-edit-protocol

ファイル編集時の不変条件。`CLAUDE.md` の保護領域記述と組み合わせて適用する。

## 1. 保護領域（編集禁止）

以下のディレクトリ・ファイルは AI エージェントが直接編集してはならない。

- `assets/` — ライセンス管理領域。人間が手動管理
- `LICENSES/` — ライセンス本文。人間が手動管理
- `REUSE.toml` — ライセンスメタデータ
- `.github/workflows/` — CI 定義。変更は人間レビュー必須

`assets/` の追加・差し替えが必要な場合は Issue コメントで人間に依頼する。

## 2. 最小差分の原則

- 受け入れ条件・Allowed Paths の範囲外には触らない
- 既存ファイルのスタイル・フォーマット・命名規則を踏襲する
- 関連しない箇所のリファクタリングを混ぜない（別 Issue にする）

## 3. 1 コミット 1 責務

- 1 コミットは 1 つの論理的な変更単位とする
- 検証コマンド（typecheck / lint / test / build）が通る状態でコミットする
- WIP コミットは push しない（push 前に rebase / squash で整理）

## 4. ステージング

- `git add -A` / `git add .` は使わない
- 変更ファイルのパスを明示して `git add <path>` する
- 意図しないファイル（.env、IDE 設定、ローカル一時ファイル等）の混入を防ぐ

## 5. `--no-verify` 禁止

Git Hooks をすり抜けるオプションは使わない。詳細は [`git-policy`](git-policy.md)。
