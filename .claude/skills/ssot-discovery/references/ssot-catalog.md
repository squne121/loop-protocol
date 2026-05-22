# SSOT カタログ（派生キャッシュ）

<!--
derived_from: docs/dev/ssot-registry.md
このファイルは docs/dev/ssot-registry.md から派生するキャッシュである。
SSOT カタログの正本は docs/dev/ssot-registry.md を参照すること。
新規エントリ追加・削除は docs/dev/ssot-registry.md を先に更新し、本ファイルを同期すること。
-->

`docs/` 配下を SSOT として扱う際のキャッシュ。
カタログの正本は `docs/dev/ssot-registry.md` であり、本ファイルは Skill 層からの参照用に維持される。
本カタログを更新したら `docs/dev/ssot-registry.md` と `match-ssot.sh` も合わせて更新する。

## 開発運用 (`docs/dev/`)

| パス | 主題 | 主要キーワード |
|---|---|---|
| `docs/dev/workflow.md` | Issue 駆動開発フロー全体（SSOT） | workflow, ssot, hooks, ci, worktree, issue, pr, テスト戦略, フロー |
| `docs/dev/agent-skill-boundaries.md` | SubAgent / Skill 責務境界・オーケストレーター設計原則・ループ内人間承認原則 | agent, skill, subagent, 責務, role, control-plane, data-plane, loop_state, 人間承認 |
| `docs/dev/github-ops.md` | `gh` CLI 利用規約・body-file guard・Parent Mode・コメント記録テンプレ | gh, github, ops, body-file, parent_mode, comment, label |
| `docs/dev/milestone-ops.md` | GitHub Milestone 作成・割当・close・rollup の正本 | milestone, github-milestone, milestone-ops, milestone作成, milestone割当, milestone-close, milestone-rollup |
| `docs/dev/directory-structure.md` | ディレクトリ責務 | directory, structure, src, layer, 分離 |
| `docs/dev/current-focus.md` | 現在のフェーズ・優先項目 | current, focus, phase, mvp, milestone |

## アーキテクチャ決定記録 (`docs/adr/`)

| パス | 主題 | 主要キーワード |
|---|---|---|
| `docs/adr/0001-architecture-baseline.md` | state/render/systems/ui/storage 分離・60Hz 固定タイムステップ | architecture, state, render, systems, ecs, 60hz, タイムステップ |

新規 ADR は `docs/adr/NNNN-<topic>.md` で追加し、本表に追記する。

## プロダクト仕様 (`docs/product/`)

| パス | 主題 | 主要キーワード |
|---|---|---|
| `docs/product/game-overview.md` | ゲーム全体像 | game, overview, シナリオ, 世界観 |
| `docs/product/requirements.md` | 要件定義 | requirements, mvp, scope, 仕様 |

## ディレクトリ → SSOT マッピング

`target_paths` 入力時に「ディレクトリと関連する SSOT」を引くための索引。

| 対象パス | 関連 SSOT |
|---|---|
| `src/state/**` | `docs/adr/0001-architecture-baseline.md` |
| `src/render/**` | `docs/adr/0001-architecture-baseline.md` |
| `src/systems/**` | `docs/adr/0001-architecture-baseline.md` |
| `src/data/**` | ルート `CLAUDE.md`、`src/data/README.md` |
| `src/storage/**` | `docs/adr/0001-architecture-baseline.md` |
| `src/ui/**` | `docs/adr/0001-architecture-baseline.md` |
| `tests/**` | `docs/dev/workflow.md`（テスト戦略 3 層） |
| `.claude/skills/**` | `docs/dev/agent-skill-boundaries.md`、`docs/dev/workflow.md` |
| `.claude/agents/**` | `docs/dev/agent-skill-boundaries.md` |
| `.github/**` | `docs/dev/github-ops.md`、`docs/dev/workflow.md` |
| `.github/workflows/**` | `docs/dev/workflow.md`（CI 層） |
| `scripts/**` | `docs/dev/workflow.md` |

## 更新ガイド

- 新規 SSOT 文書追加時 → 本カタログにエントリ追加 + `match-ssot.sh` の patterns 更新
- SSOT 文書削除時 → 本カタログから削除 + 参照していた skill / agent も同 PR で更新
