# Issue テンプレート採用記録（github-ops-*.md 評価）

別プロジェクト由来の `.github/ISSUE_TEMPLATE/github-ops-*.md` 4 件を評価し、LOOP_PROTOCOL 用に採用・除外を判断した結果の記録。

出典: https://github.com/squne121/loop-protocol/pull/12#issuecomment-4477983586

## ファイル対応

| 元ファイル | 採用先 | 主な変更 |
|---|---|---|
| `github-ops-implementation.md` | `implementation.md` | `just check` → `pnpm typecheck/lint/test/build`、Windows GUI live-verify セクション削除、`.agents/rules/` Rules セクション削除、`change_kind` に `data` を追加、`.agents/skills/` sync 注記削除 |
| `github-ops-research.md` | `research.md` | `windows-gui-dev` Required Skills 注記削除、`Required Skills` セクションを簡素化、`shared-agent-skills-governance` 参照削除 |
| `github-ops-parent.md` | `parent.md` | `#446` 等の他リポジトリ Issue 番号参照を削除、runtime auto-close 文言を簡素化 |
| `github-ops-human-confirm.md` | `human-confirm.md` | `issueops-operations` Required Skills 注記削除 |

## 採用した要素

- **Machine-Readable Contract**: skill 側で YAML パースして契約を確認できる構造（`issue_kind`、`parent_issue`、`goal_ref`、`change_kind`、`parent_mode`、`closure_mode`、`decision_type`）
- **Parent / child の階層構造**: parent → implementation / research / human-confirm の child という運用
- **Outcome / In Scope / Out of Scope / Acceptance Criteria / Stop Conditions の必須セクション**: skill 側の preflight チェックで機械判定する
- **Stop Conditions の 6 定型項目**（implementation）: nested SubAgent / 外部サービス / 大規模テスト改変等の停止条件
- **Verification Commands を AC とセットで列挙する規約**: test-runner SubAgent が自律実行できる前提
- **`agent/*` ラベル**: 担当 SubAgent を可視化
- **Quality Decision Record / Parent Closure Rule**（parent）: 親 Issue の close 条件を明文化
- **Phase Handoff Contract**（parent / research）: SubAgent 間ハンドオフの構造

## 除外した要素

- **`.agents/rules/*.md` Rules セクション**: PR #15 で `.agents/rules/` を持ち込まない方針が確定（per-dir CLAUDE.md + ssot-discovery skill 経由）。当然これも除外
- **`just check` ベースの Verification Commands**: LOOP_PROTOCOL は pnpm + Vite + Vitest のため `pnpm` スクリプトに置換
- **Windows GUI（uiautomation / pywinauto）live-verify マーカー**: LOOP_PROTOCOL はブラウザゲームのため不要
- **`.agents/skills/` sync 注記**（`bash scripts/sync-agent-skills.sh` 等）: LOOP_PROTOCOL に該当する同期スクリプトなし
- **`#446` 等の他リポジトリ Issue 番号参照**: 文脈依存のため削除
- **`shared-agent-skills-governance` / `issueops-operations` Required Skills 自動付与**: 該当 skill / governance package が LOOP_PROTOCOL に存在しない
- **`change_kind: workflow` 以外の細かい列挙**: implementation 側でも `data`（`src/data/` 配下バランス調整）を許可するため追加

## 既存テンプレートとの関係

| 既存テンプレート | 役割 | 残置 / 統合 |
|---|---|---|
| `bug-report.yml` | エンドユーザーがバグ報告 | **残置**（受け手・粒度が implementation と異なる） |
| `feature-request.yml` | エンドユーザーが機能要望 | **残置**（同上、内部の implementation child issue とは別世界） |
| `config.yml` | テンプレ選択画面の設定 | 変更なし |

新規 4 テンプレは **AI エージェント駆動の内部ワークフロー用**、既存 2 テンプレは **エンドユーザー向け** として共存させる。

## ファイル名から `github-ops-` プレフィックスを除外した理由

元プロジェクトの `github-ops-*` というプレフィックスは、その親プロジェクトでの命名規則（GitHub Ops 関連 issue を束ねる）に由来する。LOOP_PROTOCOL ではこれが意味を持たないため、シンプルに `implementation.md` / `research.md` / `parent.md` / `human-confirm.md` とした。

流用 skill が `.github/ISSUE_TEMPLATE/github-ops-{種別}.md` を参照している箇所は、フェーズ C-1（Issue 管理系適合）で本テンプレ名に置換する。

## 関連

- PR #12（フェーズ A、流用 skill 取込）
- PR #15（フェーズ B、per-dir CLAUDE.md + ssot-discovery）
- 次フェーズ C-1（Issue 管理系 skill 適合）で本テンプレを参照する形に skill を書き換える
