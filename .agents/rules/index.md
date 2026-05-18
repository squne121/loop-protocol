# Rules Index

`.agents/rules/` 配下の rule 一覧。SubAgent への inline 注入時の正本としても使う。

| rule-id | 役割 | 主な参照元 |
|---|---|---|
| `file-edit-protocol` | Edit/Write 時の保護領域・最小差分・1コミット1責務 | implement-issue / impl-review-loop / pr-review-judge |
| `git-policy` | `1 Issue = 1 PR`、worktree 配置、`--no-verify` 禁止、push 規約 | implement-issue / impl-review-loop / issue-refinement-loop / pr-review-judge |
| `github-ops-workflow` | gh CLI 利用パターン、`tmp/` 経由 body-file 更新、ラベル運用 | 多数 |
| `issue-body-ssot-policy` | Issue 本文を契約の SSOT として扱う原則 | issue-contract-review |
| `issue-uncertainty-policy` | `phase/research`・`state/needs-human` の付与基準 | create-issue / implement-issue / impl-review-loop / issue-refinement-loop |
| `issueops-common-guard` | Issue/PR 書き込み時の共通ガード（assignment、labels、template） | 多数 |
| `issueops-mode-guard` | research vs implementation の判定とラベル整合 | issue-contract-review |
| `orchestrator-skill-policy` | オーケストレーター skill の共通契約（state tracking、終了条件） | impl-review-loop / issue-refinement-loop |
| `skill-rule-boundary` | skill 本文 vs rule の責務境界（rule は不変条件、skill は手順） | implement-issue / impl-review-loop |
| `skill-sync-policy` | skill 改修時の整合性確認（required_rules / cross-skill 参照） | implement-issue / impl-review-loop |
| `subagent-design-policy` | SubAgent 設計共通指針（権限最小化、コンテキスト隔離、出力契約） | impl-review-loop / issue-refinement-loop |

## 設計方針

- 各 rule は **LOOP_PROTOCOL に必要な不変条件だけ** を簡潔に記述する（外部ボイラープレートを引きずらない）。
- `CLAUDE.md`（プロジェクト憲法）と内容が重複する場合は CLAUDE.md を優先し、rule では参照のみとする。
- skill が `required_rules: [<id>]` で参照する rule は本ディレクトリに必ず実体があること（skill-sync-policy で確認）。

## 削除済み（流用元には存在したが LOOP_PROTOCOL では不要）

- `wsl-dev-environment` — LOOP_PROTOCOL は WSL 固有の依存なし
- `xxx` — プレースホルダのため不要
