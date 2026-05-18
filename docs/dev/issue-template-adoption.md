# Issue テンプレート採用記録（github-ops-*.md 評価）

別プロジェクト由来の `.github/ISSUE_TEMPLATE/github-ops-*.md` 4 件を評価し、LOOP_PROTOCOL 用に採用・除外を判断した結果の記録。

出典: https://github.com/squne121/loop-protocol/pull/12#issuecomment-4477983586
レビュー反映: https://github.com/squne121/loop-protocol/pull/16#issuecomment-4478704988

## ファイル対応

| 元ファイル | 採用先 | 形式 | 主な変更 |
|---|---|---|---|
| `github-ops-implementation.md` | `implementation.yml` | Issue Forms | `just check` → `pnpm typecheck/lint/test/build`、Windows GUI live-verify セクション削除、`.agents/rules/` Rules セクション削除、`change_kind` に `data` 追加、`.agents/skills/` sync 注記削除、**Required Skills を「ドメイン知識スキル」用に再定義** |
| `github-ops-research.md` | `research.yml` | Issue Forms | `windows-gui-dev` Required Skills 注記削除、Outcome を「**次のアクションに進める状態**」に強化、Allowed Paths 既定を「読み取り専用 + 次 Issue 起票が成果物」に変更 |
| `github-ops-parent.md` | `parent.yml` | Issue Forms | `#446` 等の他リポジトリ Issue 番号参照を削除、runtime auto-close 文言を簡素化 |
| `github-ops-human-confirm.md` | **不採用（削除）** | — | 別 Issue 化せず、元 Issue 内で人間回答をブロッカー扱い → 本文修正 → 実装開始 の運用とする |

## 形式: Issue Forms (`.yml`) を採用した理由

PR #16 レビュー（comment 4478704988）で「AI エージェントの起票にハーネスをつける実装は導入されているか」と問われた。`.md` テンプレートでは必須項目の機械検証ができないため、`.yml`（Issue Forms）に切り替えて以下を担保する：

- GitHub サーバ側で `validations.required` が強制される（必須項目が空のまま submit できない）
- 各セクションが固定の Markdown 見出し（`### <Label>`）として出力されるため、skill 側の正規表現マッチが安定する
- フィールド ID で参照できるため、将来的にプログラム的なパースが容易

## 採用した要素

- **Machine-Readable Contract**: skill 側で YAML パースして契約を確認できる構造
- **Parent / child の階層構造**: parent → implementation / research の child
- **Outcome / In Scope / Out of Scope / Acceptance Criteria / Stop Conditions の必須セクション**: skill 側の preflight チェックで機械判定
- **Stop Conditions の 6 定型項目**（implementation）
- **Verification Commands を AC とセットで列挙**: test-runner SubAgent が自律実行できる前提
- **`agent/*` ラベル**: 担当 SubAgent を可視化
- **Quality Decision Record / Parent Closure Rule**（parent）
- **Phase Handoff Contract**（parent / research）

## レビュー反映（PR #16 comment 4478704988）

| Discussion | 反映内容 |
|---|---|
| r3259617650 | `human-confirm.md` を**削除**。別 Issue 化せず元 Issue で人間判断ブロッカー扱いの運用に切替 |
| r3259628367 | `.md` → `.yml` Issue Forms へ変換（implementation / research / parent 3 件）。`validations.required` でハーネス化 |
| r3259647395 | `Required Skills` の説明を**変更**。ワークフロー skill（issue-contract-review / implement-issue 等）を除外し、**ドメイン知識スキル**（TypeScript / ECS / Canvas / Vitest BDD 等）のみを書く規約に |
| r3259676730 | `research.yml` の Outcome を「**次のアクションに進める状態**」必須化。揮発的な比較表だけで閉じる運用を不可に |
| r3259689724 | `research.yml` の Allowed Paths を「既定: 読み取り専用、次 Issue 起票が成果物」に変更。リポジトリ資産出力は例外用途 |

## 除外した要素

- **`.agents/rules/*.md` Rules セクション**: PR #15 で `.agents/rules/` 不採用が確定（per-dir CLAUDE.md + ssot-discovery 経由）
- **`just check` ベースの Verification Commands**: pnpm に置換
- **Windows GUI live-verify マーカー**: LOOP_PROTOCOL はブラウザゲーム
- **`.agents/skills/` sync 注記**: 該当機構なし
- **他リポジトリ Issue 番号参照**
- **`shared-agent-skills-governance` / `issueops-operations` Required Skills 自動付与**: governance package なし

## 既存テンプレートとの関係

| 既存 | 役割 | 扱い |
|---|---|---|
| `bug-report.yml` | エンドユーザーのバグ報告 | **残置**（受け手・粒度が implementation と異なる） |
| `feature-request.yml` | エンドユーザーの機能要望 | **残置** |
| `config.yml` | テンプレ選択画面の設定 | **変更なし** |

新規 3 テンプレ（implementation / research / parent）は AI エージェント駆動の **内部ワークフロー用**。既存 2 テンプレは **エンドユーザー向け** として共存。

## 次フェーズ依存

流用 skill が `.github/ISSUE_TEMPLATE/github-ops-{種別}.md` を参照している箇所は、**フェーズ C-1（Issue 管理系適合）** で本テンプレ名（`implementation.yml` / `research.yml` / `parent.yml`）に置換する。

## 関連

- PR #12（マージ済み）: フェーズ A — 流用 skill / agent import
- PR #15（マージ済み）: フェーズ B — per-dir CLAUDE.md + ssot-discovery
- PR #12 comment 4477983586: 本テンプレ提供元
- PR #16 comment 4478704988: レビュー指摘（Issue Forms 化 / human-confirm 削除 / Required Skills 意味変更 / research 強化）
