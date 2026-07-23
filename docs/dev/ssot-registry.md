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

- id: execution-planning-policy
  path: docs/dev/workflow.md
  title: Execution Planning Policy (canonical SSOT)
  keywords: [execution-planning, issue-execution-decision, semantic-planning, collision-derived, dual-write, consumer-compatibility]
  description: ISSUE_EXECUTION_DECISION_V1 の静的契約、namespace 分離、migration、open-pr hard gate 維持の正本
  sections:
    - "## Execution Planning Policy (canonical SSOT)"

- id: agent-skill-boundaries
  path: docs/dev/agent-skill-boundaries.md
  title: Agent / Skill 責務境界
  keywords: [agent, skill, subagent, 責務, role, control-plane, data-plane, loop_state, 人間承認, parallel, concurrent, race, 並列実行, volatile-roots]
  description: SubAgent / Skill 責務境界・オーケストレーター設計原則・ループ内人間承認原則・並列エージェント実行安全性
  sections:
    - "## SubAgent 役割分類と permissionMode 一覧"
    - "## オーケストレーター設計原則"
    - "## Loop Sequencing & Preconditions"
    - "## Parallel Agent Runtime Safety"

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
  title: Runtime Verification Policy（動作検証ポリシー）
  keywords: [runtime, verification, policy, skip, exit77, immediate, deferred, not_applicable]
  description: 動作検証 AC の運用規約

- id: workflows-design-docs-refinement
  path: docs/dev/workflows/issue-refinement-loop-design.md
  title: issue-refinement-loop 詳細設計ノート（derived_design_note）
  keywords: [issue-refinement-loop, planner, state-machine, loop-state, escalation]
  description: |
    issue-refinement-loop の詳細設計ノート（ssot_classification: derived_design_note）。
    canonical_sources の正本と矛盾した場合は正本が勝つ（conflict_rule: canonical_sources_win）。
  sections:
    - "## State Model"
    - "## Failure Modes and Recovery"
    - "## SubAgent Contract Matrix"

- id: workflows-design-docs-impl-review
  path: docs/dev/workflows/impl-review-loop-design.md
  title: impl-review-loop 詳細設計ノート（derived_design_note）
  keywords: [impl-review-loop, subagent-contract, loop-state, escalation, state-machine]
  description: |
    impl-review-loop の詳細設計ノート（ssot_classification: derived_design_note）。
    canonical_sources の正本と矛盾した場合は正本が勝つ（conflict_rule: canonical_sources_win）。
  sections:
    - "## State Model"
    - "## Failure Modes and Recovery"
    - "## SubAgent Contract Matrix"

- id: release-distribution-policy
  path: docs/dev/release-distribution-policy.md
  title: リリース・配布方針
  keywords: [release, distribution, rc, vite, github-pages, itch.io, dist, 配布, リリース, RC, build-artifact]
  description: Vite build 成果物の取扱方針、配布候補プラットフォームの評価、M1 RC checklist の正本。status は draft（mvp-scope.md が accepted になるまで public release は blocked）
  lifecycle_status: draft
  scope: "M1 Foundation Gate (v0.1.x)"
  sections:
    - "## Vite Build Artifact Policy"
    - "## Distribution Candidate Matrix"
    - "## Target-Specific Constraints"
    - "## Release Candidate Checklist"

- id: product-spec-lifecycle
  path: docs/dev/product-spec-lifecycle.md
  title: Product Spec Lifecycle（プロダクト仕様のライフサイクル）
  keywords: [product-spec, lifecycle, docs/product, compact-spec, scoped-loading, diff-first, token-policy, ears, spec-delta, tasks-md, staging-artifact, archive, supersede, ssot-registry, registry-entry, directory-mapping]
  description: docs/product/** の作成・更新・archive・supersede・registry 登録・compact spec・diff-first 更新・EARS 採用・playtest feedback から spec delta issue への変換。workflow.md との責務境界定義を含む
  sections:
    - "## Authority / Responsibility Boundary"
    - "## Product SSOT Taxonomy"
    - "## Lifecycle States"
    - "## Creation Rules"
    - "## Token Policy"
    - "## Product Spec Delta Flow"
    - "## tasks.md Adapter"
    - "## Registry / Discovery Rules"

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
  description: SDD ツール採否（Spec Kit upstream-compatible / accepted・confirmed by #303）・正本境界・conflict rule・tasks.md staging・namespace policy・token 対策・playtest 補正
  sections:
    - "## 決定"
    - "## Decision Points"
    - "## 結果と影響"

新規 ADR は `docs/adr/NNNN-<topic>.md` で追加し、本表にエントリを追加する。

---

## プロダクト仕様 (`docs/product/`)

- id: movement-projectile
  path: docs/product/features/movement-projectile.md
  title: Movement + Projectile 最小仕様（移動と発射体の最小仕様）
  keywords: [movement, projectile, combat, player, aim, fire, cooldown, arena, coordinate, diagonal_normalization, boundary_clamp, PointerEvent, GameState.projectiles, speed_px_per_sec, lifetime_ms, monotonic_counter, fixed_timestep, 60hz, raf]
  description: player 移動・aim・fire・projectile の最小ライフサイクルを固定する feature spec（status: draft）。#1 実装の前提仕様
  sections:
    - "## intent"
    - "## requirements"
    - "## acceptance_criteria"
    - "## non_goals"
    - "## open_questions"
    - "## playtest_hypotheses"
    - "## related_tests"

- id: feature-sortie
  path: docs/product/features/sortie.md
  title: Sortie Lifecycle & Combat End Conditions
  keywords: [sortie, lifecycle, fsm, state-machine, timer, elapsedTicks, targetTicks, fixed_timestep, result-object, victory, defeat, combat, end-conditions, transient-output]
  description: 戦闘（Sortie）の開始から終了（120秒生存または自機HP0）までの状態遷移、固定タイムステップタイマーの管理、および Transient Result Object の生成ルールの正本。永続化やUI表示に関する定義は持たない。
  sections:
    - "## Sortie State Machine (FSM)"
    - "## End Conditions"
    - "## Timer Authority"
    - "## SortieResult"
    - "## Non-Goals"
    - "## planned_consumer_tests"

- id: game-overview
  path: docs/product/game-overview.md
  title: ゲーム全体像
  keywords: [game, overview, シナリオ, 世界観, ゲーム概要]
  description: ゲーム全体像（概念説明。要件正本として扱わない）

- id: game-thesis
  path: docs/product/game-thesis.md
  title: Game Thesis
  keywords: [game-thesis, concept, pitch, target player, design pillars, non-goals, design hypotheses, mda]
  description: ゲームのコアコンセプト、ターゲット、設計の柱、および設計仮説を定義するプロダクト仕様の正本
  sections:
    - "## 状態注記 / Status Note"
    - "## ピッチ / Pitch"
    - "## 想定プレイヤー / Target Player"
    - "## 設計の柱 / Design Pillars"
    - "## 非ゴール / Non-Goals"
    - "## 設計仮説 / Design Hypotheses"
    - "## 目的 / Intent"
    - "## 未解決の問い / Open Questions"
    - "## プレイテスト仮説 / Playtest Hypotheses"
    - "## 受け入れ条件境界 / Acceptance Criteria Boundary"
    - "## トレースリンク / Trace Links"

- id: requirements
  path: docs/product/requirements.md
  title: 要件定義
  keywords: [requirements, mvp, scope, 仕様, 非ゴール, 要件]
  description: 全体要件と非ゴールの正本

- id: game-design
  path: docs/product/game-design.md
  title: Game Design Document (GDD v0.1)（ゲームデザイン文書）
  keywords: [game-design, gdd, core-loop, sortie-loop, screens, progression, rewards, non-goals, downstream-boundaries, open-questions, playtest-hypotheses, design-pillars, localized-intervention, reverse-engineering, analysis-data, combat-readability, compact-spec, ears]
  description: GDD-level design の正本。Core Loop / Sortie Loop / Screens / Progression / Rewards / Non-Goals / Downstream Boundaries / Open Questions / Playtest Hypotheses（コアループ・出撃ループ・画面・進行・報酬・非ゴール・下流境界・未解決の問い・プレイテスト仮説）を保持し、game-logic.md / mvp-scope.md / playtest-protocol.md の上位制約として機能する（実装定数は委譲、game-thesis.md 未マージ時は fallback draft）
  sections:
    - "## Intent"
    - "## Authority and Fallbacks"
    - "## Design Pillars"
    - "## Requirements"
    - "## Core Loop"
    - "## Sortie Loop"
    - "## Screens"
    - "## Progression"
    - "## Rewards"
    - "## Non-Goals"
    - "## Downstream Boundaries"
    - "## Open Questions"
    - "## Playtest Hypotheses"

- id: game-logic
  path: docs/product/game-logic.md
  title: Game Logic Specification
  keywords: [game-logic, state-transition, input, fixed-timestep, accumulator, collision, ccd, persistence, snapshot, victory, defeat, deterministic-test]
  description: 状態遷移・入力正規化・60Hz 固定タイムステップ・衝突・勝敗・保存境界を定義するゲームロジック仕様の正本
  sections:
    - "## 目的 / Intent"
    - "## 正本階層 / Authority and Fallbacks"
    - "## 要求 / Requirements"
    - "## 状態遷移 / State Transitions"
    - "## 入力 / Input"
    - "## 時間モデル / Time Model"
    - "## 衝突 / Collision"
    - "## 勝敗 / Victory, Defeat, Draw"
    - "## 保存境界 / Persistence Boundary"
    - "## 非ゴール / Non-Goals"
    - "## 下流境界 / Downstream Boundaries"
    - "## 未解決の問い / Open Questions"
    - "## 検証 / Verification Notes"

- id: mvp-scope
  path: docs/product/mvp-scope.md
  title: MVP Scope Definition（MVP スコープ定義）
  keywords: [mvp-scope, mvp, scope, hypotheses, success-criteria, failure-criteria, pivot-criteria, playtest, downstream-boundaries]
  description: MVP に含める / 含めない境界、検証仮説、success / failure / pivot criteria（成功基準・失敗基準・方向転換基準）を定義する draft product spec。status: accepted になるまでは実装判断の normative source ではない
  sections:
    - "## 状態注記 / Status Note"
    - "## 目的 / Intent"
    - "## 正本階層 / Authority and Fallbacks"
    - "### Normativity Guard"
    - "## MVP Hypotheses"
    - "## Included"
    - "## Excluded"
    - "## Success Criteria"
    - "## Failure Criteria"
    - "## Pivot Criteria"
    - "## Measurement Contract"
    - "## Non-Goals"
    - "## Downstream Boundaries"
    - "## MVP Tunable Parameters"
    - "## Open Questions"
    - "## Playtest Handoff"
    - "## Trace Links"

- id: playtest-protocol
  path: docs/product/playtest-protocol.md
  title: Playtest Protocol
  keywords: [playtest, protocol, session-planning, feedback, spec-delta-gate, privacy, pii]
  description: プレイテストの実施手順、フィードバック分類、Spec Delta Gate、およびプライバシー保護方針を定義する SSOT。status: draft の間は implementation normative ではない
  sections:
    - "## Session Planning"
    - "## Participant / Tester Handling"
    - "## Task Script"
    - "## Observation Rules"
    - "## Feedback Classification"
    - "## Spec Delta Gate"
    - "## Decision Meeting"
    - "## Privacy / PII Handling"

- id: playtest-log
  path: docs/product/playtest-log.md
  title: Playtest Log Template
  keywords: [playtest, log, template, schema, entry]
  description: プレイテスト結果を記録するための YAML テンプレートとスキーマ定義の SSOT。status: draft の間は implementation normative ではない
  sections:
    - "## Entry Schema"
    - "## Example Entry"

- id: combat-core
  path: docs/product/features/combat-core.md
  title: Combat Core Feature Spec（戦闘コア機能仕様）
  keywords: [combat, collision, hitbox, enemy, projectile, damage, defeat, CombatSystem, CollisionSystem, CollisionPair, EnemyState, contactDamage, defeatedAtTick, M2]
  description: M2 Combat MVP Gate の前提仕様。CollisionSystem/CombatSystem 責務境界・CollisionPair 型・EnemyState フィールド・defeat policy・deterministic order の正本（衝突・当たり判定・撃破ロジックと決定論的処理順序の仕様）
  sections:
    - "## Non-Goals"
    - "## EnemyState"
    - "## CollisionSystem Contract"
    - "## CombatSystem Contract"
    - "## Defeat Policy"

- id: feature-quick-save
  path: docs/product/features/quick-save.md
  title: Quick Save Feature Spec
  keywords:
    - quick-save
    - quick-load
    - localStorage
    - GameSnapshot
    - progression
    - save-load
    - resources
    - weaponPower
    - playerMaxHp
    - preparation-phase
  description: Quick Save / Load の progression-only 保存境界、現行 localStorage 実装、現行 boot-time load 動作、保存対象外フィールド、storage 失敗モード、将来実装 issue を定義する。

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
  - pattern: "docs/product/**"
    ssots:
      - docs/dev/product-spec-lifecycle.md
      - docs/product/requirements.md
      - docs/adr/0002-sdd-tool-adoption.md
  - pattern: "docs/dev/workflows/**"
    ssots:
      - docs/dev/workflow.md
      - docs/dev/agent-skill-boundaries.md
      - docs/dev/ssot-registry.md
```

---

## Derived Artifacts（派生成果物）

`docs/` SSOT の下位に位置する derived workbench artifacts を管理する。
derived artifact は `docs/` SSOT に矛盾した場合、`docs/` が勝つ（conflict_rule: docs-ssot-wins）。

| Path | Source | Role | 生成方法 | 注意事項 |
|------|--------|------|----------|----------|
| `.specify/` | specify-cli v0.8.13 upstream | derived workbench artifact | throwaway spike (#298) で `specify init --here --no-git --integration claude --force` を実行し、手動マージ（Issue #303） | 直接 `specify init` による再生成禁止。ADR 0002 `direct_speckit_implement_on_main: prohibited` 準拠。`.specify/memory/constitution.md` を docs/ SSOT の上位に置くことも禁止。 |
| `.claude/skills/speckit-*/` | specify-cli v0.8.13 upstream | reviewed upstream snapshot | throwaway spike (#298) の成果物を Issue #303 で手動マージ | upstream 名のまま維持。250 行超 SKILL.md は Tier 3 扱いで on-demand loading のみ許可（詳細は `docs/dev/agent-skill-boundaries.md`）。 |

---

## 更新ガイド

新規 SSOT 文書追加時の必須更新セット（同一 PR で実施すること）:

1. 本ドキュメント（`docs/dev/ssot-registry.md`）のみを手編集してエントリを追加する
2. `match-ssot.sh` が本ドキュメントを動的に読み取るため、エントリ追加で自動反映される
3. `.claude/skills/ssot-discovery/SKILL.md` の説明・例を更新（内容が変わった場合）

SSOT 文書削除時は本ドキュメントからエントリを削除し、参照していた skill / agent も同 PR で更新する。
