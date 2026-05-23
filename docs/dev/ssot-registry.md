# SSOT レジストリ（SSOT カタログの正本）

本ドキュメントは `docs/` 配下を SSOT として扱う際の **カタログの唯一の正本（docs 層）** である。
`match-ssot.sh` は本ドキュメントを動的に読み取り、SSOT discovery を行う。

新規 SSOT を追加した場合は、本ドキュメントのみを手編集すること。`match-ssot.sh` は本ドキュメントを動的に読むため、エントリ追加で自動反映される（発見性ギャップ防止）。

## エントリ形式

各エントリは以下のフィールドを持つ:

```
- id: <識別子>
  path: <docs/ 相対パス>
  title: <文書タイトル>
  keywords: [<comma-separated キーワードリスト>]
  description: <主題の短い説明>
  sections: [<代表的な見出し>]  # 任意
```

---

## 開発運用 (`docs/dev/`)

- id: workflow
  path: docs/dev/workflow.md
  title: LOOP_PROTOCOL 開発運用ワークフロー（SSOT）
  keywords: [workflow, ssot, hooks, ci, worktree, issue, pr, テスト戦略, フロー, 1-issue-1-pr]
  description: Issue 駆動開発フロー全体（SSOT）
  sections:
    - "## 全体像（3 階層構造）"
    - "## Issue 駆動開発フロー"
    - "## テスト戦略（3 層責務分離）"
    - "## Worktree 配置規約"

- id: agent-skill-boundaries
  path: docs/dev/agent-skill-boundaries.md
  title: Agent / Skill 責務境界
  keywords: [agent, skill, subagent, 責務, role, control-plane, data-plane, loop_state, 人間承認]
  description: SubAgent / Skill 責務境界・オーケストレーター設計原則・ループ内人間承認原則
  sections:
    - "## SubAgent 役割分類と permissionMode 一覧"
    - "## オーケストレーター設計原則"
    - "## Loop Sequencing & Preconditions"

- id: github-ops
  path: docs/dev/github-ops.md
  title: GitHub Ops 運用ルール
  keywords: [gh, github, ops, body-file, parent_mode, comment, label, issue, pr]
  description: "`gh` CLI 利用規約・body-file guard・Parent Mode・コメント記録テンプレ"
  sections:
    - "## Body File Guidance"
    - "## Parent Issue の Machine-Readable Contract"
    - "## ラベル運用"

- id: milestone-ops
  path: docs/dev/milestone-ops.md
  title: Milestone 運用規約（SSOT）
  keywords: [milestone, github-milestone, milestone-ops, milestone作成, milestone割当, milestone-close, milestone-rollup, due_on, リリース目標, フェーズ区切り]
  description: GitHub Milestone の作成・割当・close・rollup の正本。AI エージェントが Milestone 操作を行う際はこの文書を参照する
  sections:
    - "## Milestone の責務"
    - "## Milestone 命名規則"
    - "## AI エージェント操作フロー"
    - "## Milestone close 条件"

- id: directory-structure
  path: docs/dev/directory-structure.md
  title: ディレクトリ構造
  keywords: [directory, structure, src, layer, 分離]
  description: ディレクトリ責務の SSOT

- id: current-focus
  path: docs/dev/current-focus.md
  title: 現在のフェーズ・優先項目
  keywords: [current, focus, phase, mvp, milestone, 優先度, フェーズ]
  description: 現在の開発フェーズと優先順位（一時的メモ。恒久仕様に昇格しない）

- id: runtime-verification-policy
  path: docs/dev/runtime-verification-policy.md
  title: Runtime Verification Policy
  keywords: [runtime, verification, policy, skip, exit77, immediate, deferred, not_applicable]
  description: 動作検証 AC の運用規約

---

## アーキテクチャ決定記録 (`docs/adr/`)

- id: adr-0001-architecture-baseline
  path: docs/adr/0001-architecture-baseline.md
  title: アーキテクチャベースライン
  keywords: [architecture, state, render, systems, ecs, 60hz, タイムステップ, storage, ui]
  description: state/render/systems/ui/storage 分離・60Hz 固定タイムステップ
  sections:
    - "## 決定事項"
    - "## 背景・根拠"

- id: adr-0002-sdd-tool-adoption
  path: docs/adr/0002-sdd-tool-adoption.md
  title: SDD ツール採否 — Spec-Driven Development 運用方針
  keywords: [sdd, spec-driven-development, spec-kit, openspec, ears, canonical_source, docs-ssot, derived-workbench, tasks_md, staging-artifact, token-policy, compact-spec, scoped-loading, serena-mcp, playtest, feedback-loop, namespace, collision-policy]
  description: SDD ツール採否（Spec Kit upstream-compatible / accepted-with-deferral）・正本境界・conflict rule・tasks.md staging・namespace policy・token 対策・playtest 補正
  sections:
    - "## 決定"
    - "## Decision Points"
    - "## 結果と影響"

新規 ADR は `docs/adr/NNNN-<topic>.md` で追加し、本表にエントリを追加する。

---

## プロダクト仕様 (`docs/product/`)

- id: game-overview
  path: docs/product/game-overview.md
  title: ゲーム全体像
  keywords: [game, overview, シナリオ, 世界観, ゲーム概要]
  description: ゲーム全体像（概念説明。要件正本として扱わない）

- id: requirements
  path: docs/product/requirements.md
  title: 要件定義
  keywords: [requirements, mvp, scope, 仕様, 非ゴール, 要件]
  description: 全体要件と非ゴールの正本

---

## ディレクトリ → SSOT マッピング

`target_paths` 入力時に「ディレクトリと関連する SSOT」を引くための索引。
`match-ssot.sh` がこのブロックを `yaml.safe_load` で読み取る（機械可読 YAML）。

```yaml
directory_mappings:
  - pattern: "src/state/**"
    ssots:
      - docs/adr/0001-architecture-baseline.md
  - pattern: "src/render/**"
    ssots:
      - docs/adr/0001-architecture-baseline.md
  - pattern: "src/systems/**"
    ssots:
      - docs/adr/0001-architecture-baseline.md
  - pattern: "src/data/**"
    ssots:
      - docs/adr/0001-architecture-baseline.md
  - pattern: "src/storage/**"
    ssots:
      - docs/adr/0001-architecture-baseline.md
  - pattern: "src/ui/**"
    ssots:
      - docs/adr/0001-architecture-baseline.md
  - pattern: "tests/**"
    ssots:
      - docs/dev/workflow.md
  - pattern: ".claude/skills/**"
    ssots:
      - docs/dev/agent-skill-boundaries.md
      - docs/dev/workflow.md
  - pattern: ".claude/agents/**"
    ssots:
      - docs/dev/agent-skill-boundaries.md
  - pattern: ".github/**"
    ssots:
      - docs/dev/github-ops.md
      - docs/dev/workflow.md
  - pattern: ".github/workflows/**"
    ssots:
      - docs/dev/workflow.md
  - pattern: "scripts/**"
    ssots:
      - docs/dev/workflow.md
  - pattern: "docs/adr/**"
    ssots:
      - docs/dev/ssot-registry.md
      - docs/dev/workflow.md
```

---

## 更新ガイド

新規 SSOT 文書追加時の必須更新セット（同一 PR で実施すること）:

1. 本ドキュメント（`docs/dev/ssot-registry.md`）のみを手編集してエントリを追加する
2. `match-ssot.sh` が本ドキュメントを動的に読み取るため、エントリ追加で自動反映される
3. `.claude/skills/ssot-discovery/SKILL.md` の説明・例を更新（内容が変わった場合）

SSOT 文書削除時は本ドキュメントからエントリを削除し、参照していた skill / agent も同 PR で更新する。
