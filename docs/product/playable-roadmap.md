---
doc_id: DOC-ROADMAP-001
title: Post-M1 Playable Outcome Roadmap（M1後 playable outcome ロードマップ）
status: active
note: conceptual roadmap（概念ロードマップ）; not GitHub Milestone object creation（GitHub Milestone object 作成そのものは含まない）
last_updated_by_issue: 2573
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
- **Milestone boundary authority**: M2〜M8 の conceptual milestone boundary は本 roadmap の各 milestone セクションと mapping table を参照元とする。ただし global scope / global non-goals の正本は `docs/product/requirements.md` であり、本 roadmap を global 要件の正本へ格上げしない。GitHub Milestone object はこの roadmap を反映する外部メタデータとして扱う。M6〜M8 は conceptual definition のみであり、GitHub Milestone object / parent Issue の materialize は本文書のスコープ外。

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

## Conceptual Milestone to GitHub Milestone Mapping（概念マイルストーンと GitHub マイルストーンの対応表）

readback_date: 2026-09-08 # GitHub マイルストーンの読取確認を行った日

| conceptual_milestone_id | conceptual_title | github_milestone_number | github_milestone_title | mapping_status | decision_note |
|---|---|---:|---|---|---|
| M2 | M2: Gameplay Core (v0.2.x) | 4 | M2: Combat MVP Gate (v0.2.x) | mismatch_pending_rename | conceptual boundary は gameplay core、GitHub milestone object は旧 title のまま残っている。rename 判断は別スコープ。 |
| M3 | M3: Result Persistence (v0.3.x) | 3 | M3: Result Persistence (v0.3.x) | aligned | conceptual title と GitHub milestone title は一致している。formal close / readback の最終判断は `#733` 側で扱う。 |
| M4 | M4: Upgrade Loop (v0.4.x) | 2 | M4: Upgrade Loop (v0.4.x) | aligned | conceptual boundary と GitHub milestone object の readback が一致。title はライブ readback で確認済み。M4 parent tracker `#1176` は 2026-09-08 live readback で `CLOSED / COMPLETED` を確認し、M5 へ phase handoff 済み。 |
| M5 | M5: Playable Slice Hardening (v0.5.x) | 5 | M5: Playable Slice Hardening (v0.5.x) | aligned | conceptual boundary と GitHub milestone object の readback が一致。2026-09-08 に GitHub Milestone (number 5) を新規作成し、parent tracker #2572 を直接割り当てた。 |
| M6 | M6: Ace Intervention (v0.6.x) | null | null | unmapped | 2026-09-08 時点で GitHub Milestone object と parent Issue は未作成。今回の phase handoff では conceptual definition のみを追加し、GitHub 側の materialize は M5 close 後に別スコープで扱う。 |
| M7 | M7: Tech Extraction Loop (v0.7.x) | null | null | unmapped | 2026-09-08 時点で GitHub Milestone object と parent Issue は未作成。M6 close 後に別スコープで扱う。 |
| M8 | M8: Validated Vertical Slice (v0.8.x) | null | null | unmapped | 2026-09-08 時点で GitHub Milestone object と parent Issue は未作成。M7 close 後に別スコープで扱う。 |

---

## Milestone Dependency Policy（M4〜M8 依存境界）

```text
M4: Upgrade Loop
        ↓
M5: Playable Slice Hardening
        ↓
M6: Ace Intervention
        ↓
M7: Tech Extraction Loop
        ↓
M8: Validated Vertical Slice
```

- Milestone の close と、次 milestone の**主要 runtime implementation** の開始は直列である。次 milestone の主要 runtime implementation は、原則として current milestone close 後の current-main から開始する。
- ただし次 milestone に関する以下の作業は、current milestone 進行中でも**先行並列可能**とする（先行 runtime merge を許可する意味ではない）。
  - research（web / codebase research を含む）
  - product spec draft / review
  - adversarial review
  - Issue decomposition
  - issue-refinement-loop
  - feasibility spike
- この境界は M5〜M8 の各 milestone セクションの `dependencies` と close_conditions に反映し、次 milestone の core mechanic を current milestone の close 条件として先取りしない（例: M5 の close_conditions は M6 の ally/assist-player runtime を要求しない）。

---

## Conceptual Milestones（概念マイルストーン）

### M2: Gameplay Core (v0.2.x) / ゲームプレイ基盤

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
  - 'victory: `enemies.length > 0` guard を満たしたスポーン済み敵機をすべて撃破した場合に成立し、`SortieResult.outcome` / `SortieResult.endReason` は `victory` / `all_enemies_defeated` とする'
  - 'timeout: 30 秒到達時に成立する neutral terminal であり、`SortieResult.outcome` / `SortieResult.endReason` は `timeout` / `timeout` とする'
  - 'defeat: `player_hp_zero`（HP0）で成立し、`SortieResult.outcome` / `SortieResult.endReason` は `defeat` / `player_hp_zero` とする'
  - 'terminal_priority: defeat > victory > timeout'
  - '`survival_timer` を起点にした生存時間ベースの勝利条件は M2 の正本定義として採用しない'
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

### M3: Result Persistence (v0.3.x) / 結果保存

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

### M4: Upgrade Loop (v0.4.x) / 強化ループ

```yaml
milestone_id: M4
title: "M4: Upgrade Loop (v0.4.x)"
github_milestone_number: 2
github_milestone_title: "M4: Upgrade Loop (v0.4.x)"
mapping_status: aligned
decision_note: "conceptual boundary と GitHub milestone object の readback が一致。title はライブ readback を実施済み。"
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

### M5: Playable Slice Hardening (v0.5.x) / プレイ可能スライス硬化

```yaml
milestone_id: M5
title: "M5: Playable Slice Hardening (v0.5.x)"
github_milestone_number: 5
github_milestone_title: "M5: Playable Slice Hardening (v0.5.x)"
mapping_status: aligned
decision_note: "conceptual boundary と GitHub milestone object の readback が一致。2026-09-08 に GitHub Milestone (number 5) を新規作成し、parent tracker #2572 を直接割り当てた。"
parent_issue: 2572
source_mvp_loop:
  - dom_canvas_separation
  - result_resource_loop
scope: |
  M2〜M4で成立した sortie → result/resource → upgrade → next sortie の playable slice を、
  新しい core mechanic を増やさず安定して繰り返し遊べる状態へ硬化するフェーズ。対象:
  M4→M5 phase handoff、player-facing normal flow、HUD / combat readability、
  result / preparation UX、save / load / reload continuity、player-blocking balance / tuning、
  supported viewport / browser runtime、applicable automated regression / E2E / VRT、
  current-main に束縛した developer-self playtest。
dependencies:
  - M4: Upgrade Loop (v0.4.x) — M2〜M4 の実装が完了し、一連の loop が成立していること（parent tracker #1176 は CLOSED / COMPLETED、2026-09-08 live readback で確認）
close_conditions:
  - M4 completion が live state で確認できる
  - M4→M5 SSOT handoff が同期済み
  - GitHub M5 Milestone mapping が live readback と一致
  - fresh state から通常 UI のみで preparation → sortie → result → resource → upgrade → next sortie を完遂できる
  - saved state / reload / Load Game 後も progression と upgrade が維持され次 sortie へ反映される
  - developer-self human_internal playtest を 3〜5 sortie 完遂できる
  - normal progression に debug UI / storage 手編集 / test-only seed を必要としない
  - player-blocking readability / focus / overflow / interaction gap が残っていない
  - Canvas / DOM 分離、systems boundary、60Hz fixed timestep、snapshot storage boundary を維持
  - current canonical quality gates（pnpm typecheck / lint / test / build）と applicable runtime verification が PASS
  - M5 baseline で close blocker と判定した gap が全て解消済み
  - M6 以降の core mechanic を M5 完了のために先取りしていない
non_goals:
  - ally NPC / assist_player の新規 core runtime
  - full RTS
  - command queue / formation / pathfinding
  - M7 technology-choice expansion
  - 大規模な upgrade tree
  - campaign / territory / world map
  - base building
  - network / multiplayer
  - 本格的な audio 実装
  - 高品質アセット前提の演出
  - external tester recruitment
  - Release Candidate 化
  - 新規 dashboard / telemetry backend / control-plane
spec_destination:
  - docs/product/features/ui-information-architecture.md — HUD / combat readability の詳細仕様
  - docs/product/features/persistence.md — save / load / reload continuity の詳細仕様
```

---

### M6: Ace Intervention (v0.6.x) / エース介入

```yaml
milestone_id: M6
title: "M6: Ace Intervention (v0.6.x)"
github_milestone_number: null
github_milestone_title: null
mapping_status: unmapped
decision_note: "GitHub Milestone object と parent Issue は未作成。conceptual definition のみ。M5 closed/completed 後に materialize を別スコープで検討する。"
source_mvp_loop:
  - result_resource_loop
scope: |
  プレイヤーが「自分の直接介入によって局所戦況が変わった」と知覚できる戦闘構造を成立させる。
  最小 ally NPC と単一の軽量 macro intent を導入し、full RTS へ拡張せず、
  accepted Game Thesis の HYP-001-ace-intervention を playable runtime で検証可能にする。
dependencies:
  - M5: Playable Slice Hardening (v0.5.x) — closed/completed 後に主要 runtime implementation を開始する
  - research / spec / issue-refinement は M5 進行中に先行可能
close_conditions:
  - ally/NPC/command の stable spec が implementation authority を持つ
  - 最小 1 種類の ally archetype
  - semi-autonomous ally behavior
  - macro command intent 最大 1 種類
  - command 有無による局所戦況差を deterministic evidence で観測可能
  - player combat agency が ally AI に奪われていない
  - developer-self 3〜5 sortie で HYP-001 を評価
  - invalidated hypothesis があれば Spec Delta を先に処理
  - M5 progression / persistence / readability に回帰なし
  - canonical quality / runtime gates PASS
non_goals:
  - multi-select
  - command queue
  - formation
  - direct RTS movement command
  - pathfinding / navmesh
  - multiple command intents
  - full behavior tree / GOAP
  - base building
  - territory
  - campaign
  - multiplayer
spec_destination:
  - docs/product/features/unit-operations-and-npc-behavior.md — ally NPC / macro command intent の詳細仕様
```

---

### M7: Tech Extraction Loop (v0.7.x) / 技術抽出ループ

```yaml
milestone_id: M7
title: "M7: Tech Extraction Loop (v0.7.x)"
github_milestone_number: null
github_milestone_title: null
mapping_status: unmapped
decision_note: "GitHub Milestone object と parent Issue は未作成。conceptual definition のみ。M6 closed/completed 後に materialize を別スコープで検討する。"
source_mvp_loop:
  - result_resource_loop
scope: |
  M4で成立した generic resource / upgrade loop を、「敵技術を解析・取り込んで次戦の能力へ変える」
  progression fantasy として成立させる。numeric inflation ではなく、意味の異なる strengthening choice を選び、
  その選択が次 sortie の gameplay へ観測可能に反映される状態を目標とする。
dependencies:
  - M6: Ace Intervention (v0.6.x) — closed/completed 後に主要 runtime implementation を開始する
  - research / spec / issue-refinement は M6 進行中に先行可能
close_conditions:
  - tech extraction / progression choice の stable spec
  - sortie result が enemy technology / analysis として player-facing に読める
  - 少なくとも 2 つの意味の異なる strengthening choice
  - choice が次 sortie の behavior 差として観測可能
  - choice persistence / reload continuity
  - atomic save semantics 維持
  - developer-self 3〜5 sortie で HYP-002 を評価
  - "`just numbers` / generic currency に見えるかを観測"
  - invalidated hypothesis なら Spec Delta
  - M6 ace-intervention との統合で combat readability を壊さない
  - canonical gates PASS
non_goals:
  - 大規模な upgrade tree
  - dozens of upgrades
  - skill tree
  - loot rarity system
  - crafting
  - respec / refund economy
  - complex multi-currency economy
  - campaign progression
  - multiplayer
spec_destination:
  - docs/product/features/upgrade.md — tech extraction choice の拡張仕様
  - docs/product/features/resource.md — enemy technology analysis の記録仕様
```

---

### M8: Validated Vertical Slice (v0.8.x) / 検証済み垂直スライス

```yaml
milestone_id: M8
title: "M8: Validated Vertical Slice (v0.8.x)"
github_milestone_number: null
github_milestone_title: null
mapping_status: unmapped
decision_note: "GitHub Milestone object と parent Issue は未作成。conceptual definition のみ。M7 closed/completed 後に materialize を別スコープで検討する。Release Candidate 化ではない。"
source_mvp_loop:
  - result_resource_loop
  - dom_canvas_separation
scope: |
  M5〜M7で成立した combat readability / ace intervention / tech extraction progression を
  一つの連続 play session として統合し、LOOP_PROTOCOL の core product hypothesis を検証済み
  vertical slice として固定するフェーズ。新しい大型 mechanic を追加するフェーズではなく、
  HYP-MVP-001〜003 の統合 validation / regression 解消 / player-facing flow completion を行う。
dependencies:
  - M7: Tech Extraction Loop (v0.7.x) — closed/completed（transitive に M5/M6/M7 completion を要求する）
close_conditions:
  - M5〜M7 closed/completed
  - fresh browser/profile から通常 UI のみで開始可能
  - 3〜5 sortie 内で prepare → intervene → resolve → analyze/extract → choose upgrade → persist → next sortie が成立
  - ally/lightweight command と player agency が両立
  - progression choice が次 sortie へ反映
  - reload / Load Game 継続可能
  - HYP-MVP-001 evidence
  - HYP-MVP-002 evidence
  - HYP-MVP-003 evidence
  - unresolved design hypothesis invalidated = 0
  - supported viewport / input で player-blocker = 0
  - architecture invariant 維持
  - current canonical quality / runtime gates PASS
  - roadmap / current-focus へ次 phase handoff を記録
non_goals:
  - Release Candidate 認定
  - commercial release readiness
  - store submission
  - external tester recruitment
  - campaign
  - world map
  - territory
  - base building
  - full RTS
  - 大規模 tech tree
  - multiplayer
  - full audio production
  - high-fidelity art replacement
  - analytics SaaS / telemetry platform
  - M9 大型 mechanic の先取り
spec_destination:
  - docs/product/game-thesis.md — HYP-MVP-001〜003 の統合 validation 記録先
```

---

## 利用上の注意

- **GitHub Milestone object の作成は本文書のスコープ外**。この conceptual roadmap を GitHub API で具現化する場合は、別 Issue（change_kind: github-metadata）を切り、`docs/dev/milestone-ops.md` の操作フローに従うこと。
- **feature spec への昇格**：各 milestone の `spec_destination` に記載した候補は、安定仕様が固まった時点で `docs/product/features/<feature>.md` に昇格させる。昇格前は本文書の記述が暫定スコープ定義として機能する。
- **非正本の参照元**：本文書の `source_mvp_loop` は `docs/product/game-overview.md` の MVP Loop を参照しているが、`game-overview.md` 自体は要件の正本ではない。要件の正本は `docs/product/requirements.md` とする。

---

## Maintenance Policy（保守方針）

- この文書は conceptual roadmap の正本であり、個別機能仕様の正本ではない。
- M2〜M8 の Parent Issue が materialize された時点で、対応する issue number を各 milestone YAML block の `parent_issue` に追記する（M5: `#2572`）。
- GitHub Milestone object の対応は `github_milestone_number` / `github_milestone_title` / `mapping_status` / `decision_note` を更新し、readback date を残す。
- feature spec が作成された後は、詳細仕様は `docs/product/features/<feature>.md` を正本とし、本 roadmap は概要・依存関係・到達条件のみを保持する。
