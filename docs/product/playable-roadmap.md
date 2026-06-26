---
doc_id: DOC-ROADMAP-001
title: Post-M1 Playable Outcome Roadmap
status: active
note: conceptual roadmap; not GitHub Milestone object creation
last_updated_by_issue: 1167
---

# Post-M1 Playable Outcome Roadmap

> **conceptual roadmap; not GitHub Milestone object creation**
>
> この文書は Foundation Gate（M1）後の playable outcome を conceptual milestone として記録する。
> GitHub Milestone object（GitHub API による milestone 作成・更新）は本文書のスコープ外。
> GitHub Milestone object が必要な場合は別 Issue（change_kind: github-metadata）として分離する。

## SSOT 境界

- **全体要件の正本**: `docs/product/requirements.md`
- **体験概要（非正本）**: `docs/product/game-overview.md` — MVP Loop の参照元として使用するが、要件の正本としては扱わない
- **Milestone 命名規則**: `docs/dev/milestone-ops.md`
- 個別機能の stable な仕様は `docs/product/features/<feature>.md` へ昇格させる
- **Milestone boundary authority**: M2 / M3 / M4 / M5 の conceptual milestone boundary は本 roadmap の各 milestone セクションと mapping table を参照元とする。ただし global scope / global non-goals の正本は `docs/product/requirements.md` であり、本 roadmap を global 要件の正本へ格上げしない。GitHub Milestone object はこの roadmap を反映する外部メタデータとして扱う。

---

## MVP Loop 対応表

`docs/product/game-overview.md` の MVP Loop 4 項目と本 roadmap の conceptual milestone の対応:

| MVP Loop 項目 | source_mvp_loop | 対応 Conceptual Milestone |
|---|---|---|
| 1 戦闘ごとの sortie を短時間で遊べること | sortie_playable | M2: Gameplay Core (v0.2.x) |
| プレイヤーは Canvas 上で自機を操作し、戦場へ局所介入する | canvas_player_control | M2: Gameplay Core (v0.2.x) |
| 戦闘結果は resource として残り、次の強化導線へ接続できること | result_resource_loop | M3: Result Persistence (v0.3.x) / M4: Upgrade Loop (v0.4.x) |
| UI は DOM、戦闘表示は Canvas に分離すること | dom_canvas_separation | M2〜M5 全体の invariant。特に M2/M5 の close_conditions で検証 |

---

## Conceptual Milestone to GitHub Milestone Mapping

readback_date: 2026-06-21

| conceptual_milestone_id | conceptual_title | github_milestone_number | github_milestone_title | mapping_status | decision_note |
|---|---|---:|---|---|---|
| M2 | M2: Gameplay Core (v0.2.x) | 4 | M2: Combat MVP Gate (v0.2.x) | mismatch_pending_rename | conceptual boundary は gameplay core、GitHub milestone object は旧 title のまま残っている。rename 判断は別スコープ。 |
| M3 | M3: Result Persistence (v0.3.x) | 3 | M3: Result Persistence (v0.3.x) | aligned | conceptual title と GitHub milestone title は一致している。formal close / readback の最終判断は `#733` 側で扱う。 |
| M4 | M4: Upgrade Loop (v0.4.x) | 2 | M4: UX MVP Gate (v0.4.x) | mismatch_pending_rename | conceptual boundary は upgrade loop だが、GitHub milestone object は旧 UX milestone title のまま残っている。rename は Out of Scope。 |
| M5 | M5: Playable Slice Hardening (v0.5.x) | null | null | unmapped | 2026-06-21 readback 時点で対応する GitHub milestone object は未作成。conceptual milestone のみ存在する。 |

---

## Conceptual Milestones

### M2: Gameplay Core (v0.2.x)

```yaml
milestone_id: M2
title: "M2: Gameplay Core (v0.2.x)"
github_milestone_number: 4
github_milestone_title: "M2: Combat MVP Gate (v0.2.x)"
mapping_status: mismatch_pending_rename
decision_note: "conceptual boundary は Gameplay Core だが、GitHub milestone object は旧 title Combat MVP Gate のまま。rename は別 Issue で扱う。"
source_mvp_loop:
  - sortie_playable
  - canvas_player_control
scope: |
  movement + projectile の先に、最小の敵・当たり判定・ダメージ・sortie 終了条件を定義する。
  Canvas 上での自機操作と、1 sortie を開始→操作→戦闘結果まで通すことを目標とする。
  campaign / territory / audio / network / asset polish は除外する。
dependencies:
  - M1: Foundation Gate (v0.1.x) — docs / guardrail / workflow / 最小仕様正本の整備完了
spec_prerequisites:
  - docs/product/features/movement-projectile.md
close_conditions:
  - 1 sortie を開始→操作→戦闘結果まで通せる
  - system tests と pnpm build が通る
  - src/systems から DOM / Canvas API を直接触っていない（MVP-001 遵守）
  - 固定タイムステップ 60Hz を維持（MVP-002 遵守）
  - DOM / Canvas 分離が維持されている（dom_canvas_separation invariant 遵守）
non_goals:
  - campaign / territory 管理
  - 本格的な audio 実装
  - network / multiplayer
  - 高品質アセット前提の演出
  - requirements.md の Global Non-Goals 全般
spec_destination:
  - docs/product/features/movement-projectile.md — 自機移動・射撃・弾道の詳細仕様
  - docs/product/features/combat-core.md — 敵・当たり判定・ダメージの詳細仕様
  - docs/product/features/sortie.md — sortie 開始・終了条件の詳細仕様
```

---

### M3: Result Persistence (v0.3.x)

```yaml
milestone_id: M3
title: "M3: Result Persistence (v0.3.x)"
github_milestone_number: 3
github_milestone_title: "M3: Result Persistence (v0.3.x)"
mapping_status: aligned
decision_note: "conceptual title と GitHub milestone title は一致している。formal close / readback の最終判断は #733 側。"
source_mvp_loop:
  - result_resource_loop
scope: |
  sortie result の記録、resource 保存、snapshot 保存境界、quick save / reset との整合を定義する。
  「戦闘結果が resource として残る」MVP Loop を実現する最小実装。
  src/storage を通じた snapshot 境界での永続化（MVP-004）に対応する。
dependencies:
  - M2: Gameplay Core (v0.2.x) — sortie 結果が生成されていること
spec_prerequisites:
  - docs/product/features/sortie.md
close_conditions:
  - sortie 結果が保存境界を通じて残る
  - reset / reload 後に結果が観測できる
  - localStorage を最小保存手段として使用（MVP-004 準拠）
  - pnpm typecheck && pnpm lint && pnpm test && pnpm build が通る
non_goals:
  - クラウド同期・ネットワーク越しの永続化
  - セーブスロット複数管理
  - upgrade / resource 消費（M4 のスコープ）
spec_destination:
  - docs/product/features/persistence.md — 保存境界・snapshot の詳細仕様
  - docs/product/features/resource.md — resource 定義と記録仕様
```

---

### M4: Upgrade Loop (v0.4.x)

```yaml
milestone_id: M4
title: "M4: Upgrade Loop (v0.4.x)"
github_milestone_number: 2
github_milestone_title: "M4: UX MVP Gate (v0.4.x)"
mapping_status: mismatch_pending_rename
decision_note: "conceptual boundary は Upgrade Loop だが、GitHub milestone object は旧 UX milestone title のまま。rename は Out of Scope。"
source_mvp_loop:
  - result_resource_loop
scope: |
  resource 消費、武器または能力の最小 upgrade、次 sortie への反映を実装する。
  「resource が次の強化導線へ接続できる」MVP Loop の上位実現。
  data-driven な upgrade 定義（src/data 利用、MVP-004）に対応する。
dependencies:
  - M3: Result Persistence (v0.3.x) — resource 記録が永続化されていること
spec_prerequisites:
  - docs/product/features/resource.md
close_conditions:
  - sortie → resource 獲得 → upgrade → 次 sortie での挙動変化が確認できる
  - upgrade 定義が src/data に存在する（MVP-004 遵守）
  - pnpm typecheck && pnpm lint && pnpm test && pnpm build が通る
non_goals:
  - 複雑な campaign / territory 管理
  - 大規模な upgrade ツリー
  - spec にないネットワーク対戦 upgrade
spec_destination:
  - docs/product/features/upgrade.md — upgrade 定義・消費ロジックの詳細仕様
  - docs/product/features/resource.md — resource 消費の詳細仕様（M3 spec の拡張）
```

---

### M5: Playable Slice Hardening (v0.5.x)

```yaml
milestone_id: M5
title: "M5: Playable Slice Hardening (v0.5.x)"
github_milestone_number: null
github_milestone_title: null
mapping_status: unmapped
decision_note: "2026-06-21 readback 時点で対応する GitHub milestone object は未作成。conceptual milestone としてのみ管理する。"
source_mvp_loop:
  - dom_canvas_separation
scope: |
  M2〜M4 で構築した DOM / Canvas 分離を壊さず playable slice を硬化するフェーズ。
  HUD / telemetry / balance / UX hardening を対象とする。
  高品質アセット・本格 audio は除外する。
  dom_canvas_separation invariant を M5 完了時点でも維持していることを close_conditions で確認する。
dependencies:
  - M4: Upgrade Loop (v0.4.x) — M2〜M4 の実装が完了し、一連の loop が成立していること
close_conditions:
  - M2〜M4 の一連の loop が破綻なく手動プレイできる
  - DOM / Canvas 分離が維持されている（MVP-001 遵守）
  - MVP non-goals（campaign / audio / network / 高品質アセット）を侵食していない
  - pnpm typecheck && pnpm lint && pnpm test && pnpm build が通る
non_goals:
  - 高品質アセット前提の演出
  - 本格的な audio 実装
  - network / multiplayer
  - campaign / territory 管理
  - requirements.md の Global Non-Goals 全般
spec_destination:
  - docs/product/features/hud.md — HUD / telemetry の詳細仕様
  - docs/product/features/balance.md — バランス調整の方針と仕様
```

---

## 利用上の注意

- **GitHub Milestone object の作成は本文書のスコープ外**。この conceptual roadmap を GitHub API で具現化する場合は、別 Issue（change_kind: github-metadata）を切り、`docs/dev/milestone-ops.md` の操作フローに従うこと。
- **feature spec への昇格**：各 milestone の `spec_destination` に記載した候補は、安定仕様が固まった時点で `docs/product/features/<feature>.md` に昇格させる。昇格前は本文書の記述が暫定スコープ定義として機能する。
- **非正本の参照元**：本文書の `source_mvp_loop` は `docs/product/game-overview.md` の MVP Loop を参照しているが、`game-overview.md` 自体は要件の正本ではない。要件の正本は `docs/product/requirements.md` とする。

---

## Maintenance Policy

- この文書は conceptual roadmap の正本であり、個別機能仕様の正本ではない。
- M2〜M5 の Parent Issue が materialize された時点で、対応する issue number を追記する。
- GitHub Milestone object の対応は `github_milestone_number` / `github_milestone_title` / `mapping_status` / `decision_note` を更新し、readback date を残す。
- feature spec が作成された後は、詳細仕様は `docs/product/features/<feature>.md` を正本とし、本 roadmap は概要・依存関係・到達条件のみを保持する。
