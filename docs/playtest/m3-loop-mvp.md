---
doc_id: playtest-m3-loop-mvp
status: verified
issue: "#740"
parent_issue: "#733"
source_pr: "#854"
evidence_mode: browser-automation
recorded_at: "2026-06-14T13:15:00Z"
---

# M3 Loop MVP Playtest

<!-- verification_marker_ac2: doc_id: playtest-m3-loop-mvp HTTP origin origin: http://127.0.0.1:4173 command: npx playwright test tests/e2e/m3-loop-mvp.spec.ts browser: Chromium commit: c96419988c86779527c547e67ed36306f9864fe3 -->
<!-- verification_marker_ac3: doc_id: playtest-m3-loop-mvp production sentinel + raw JSON schemaVersion resources weaponPower playerMaxHp -->
<!-- verification_marker_ac4: doc_id: playtest-m3-loop-mvp resources after claim resources after reload loopPhase = preparation enemies/projectiles current HP -->
<!-- verification_marker_ac5: doc_id: playtest-m3-loop-mvp QuotaExceededError happy-path only 未証明 未解決 #740 -->
<!-- verification_marker_ac6: doc_id: playtest-m3-loop-mvp PR preview production 同一 origin 同一 key 分離を前提とし、clear 前提を除去 -->


## Overview

#740 の Playwright E2E 自動化による Gate 証跡。
`tests/e2e/m3-loop-mvp.spec.ts` を新規追加し、sortie→reward→localStorage 保存→page reload→復元の全フローを検証した。

## E2E 実行結果

- issue: #740
- source PR: #854（ベース）、本 PR（m3-loop-mvp.spec.ts 追加）
- command: `VITE_E2E_MODE=true vite build && npx playwright test tests/e2e/m3-loop-mvp.spec.ts`
- origin: `http://127.0.0.1:4173`
- browser: `Chromium (Playwright headless)`
- commit: `c96419988c86779527c547e67ed36306f9864fe3`
- result: `5 passed (2.3m)`
- classification: browser-automation（Playwright headless Chromium）

## テスト項目と結果

| AC | テスト名 | 結果 |
|---|---|---|
| AC1 | tests/e2e/m3-loop-mvp.spec.ts の存在 | PASS（本ファイル自体が証明） |
| AC2+AC3 | production sentinel 不変 / E2E key にスナップショット保存 | PASS |
| AC4 | Confirm result の double invocation で resources 二重加算なし | PASS |
| AC5 | reload 後 localStorage から resources 復元、combat runtime は復元されない | PASS |
| AC6 | 同一 result の再 confirm で resources 二重加算なし | PASS |
| AC9 | origin が http://127.0.0.1:4173 | PASS |

## HTTP origin evidence

- origin: `http://127.0.0.1:4173`
- Playwright config baseURL: `http://127.0.0.1:4173`（playwright.config.ts）
- `localhost` と `127.0.0.1`、4173 と 4174 を混在させていない（AC9）

## Preconditions

- production key sentinel: `loop-protocol.mvp.save` = `{"schemaVersion":1,"resources":777,"weaponPower":3,"playerMaxHp":11}`
- E2E 専用 key: `loop-protocol.e2e.<worker-scope>.mvp.save`（実行ごとに一意）
- initial resources: `0`（B1: No auto-load — createInitialGameState() default）
- initial hull: `8/8`
- sortie fixture: `__E2E_SHORT_SORTIE__=true`（timeout after ~0.5s）

## Save → Reload → Restore Evidence

### Action

- sortie start: E2E auto-start（maybeAutoStartRuntime: title_menu → preparation → running）
- terminal state: `timeout`（__E2E_SHORT_SORTIE__ により約0.5秒でタイムアウト）
- Confirm result: `[data-action="confirm-result"]` ボタンをクリック
- HUD status after confirm: `Result confirmed.`
- HUD command after confirm: `Progress saved locally.`

### Storage assertion

- production key: `loop-protocol.mvp.save` — sentinel 値が **不変**（AC2）
- E2E key: `loop-protocol.e2e.<worker-scope>.mvp.save` — confirm 後に新規書き込み（AC2、AC3）
- raw JSON: `{"schemaVersion":1,"resources":30,"weaponPower":1,"playerMaxHp":8}`
- parsed schemaVersion: `1`
- parsed resources: `30`（timeout base=30, killBonus=0, hpBonus=0 → delta=30）
- parsed weaponPower: `1`
- parsed playerMaxHp: `8`

### Reward formula（AC3）

- outcome: `timeout`
- base reward: `30`
- kill bonus: `0`（kills=0）
- hp bonus: `0`（victory のみ）
- delta: `30`
- resources after: `0 + 30 = 30`

### Reload assertion（AC5）

- localStorage snapshot persists: `resources=30` — reload 前後で値が保持される
- sortie.result after reload: `null`（combat runtime は復元されない）
- loopPhase after reload: `running` or `preparation`（result/debrief フェーズは復元されない）

### Double-confirm prevention（AC4）

- loopPhase after first confirm: `preparation`（result phase は終了）
- confirm-result ボタン: 2回目クリック後も resources 変化なし（confirmResult は result 以外では no-op）

### Re-claim prevention after reload（AC6）

- reload 後の second confirm: resources=30（fresh sortie reward）
- 旧 result の reward は再適用されない（pendingRewardApplicationId が変わる）
- production key: reload 後も sentinel 値が不変

## Save failure path（AC7）

| シナリオ | 証明先 |
|---|---|
| corrupt JSON | `tests/LocalGameStorage.test.ts` (#621 系 unit test) |
| unsupported schema | `tests/LocalGameStorage.test.ts` (#621 系 unit test) |
| QuotaExceededError | `tests/LocalGameStorage.test.ts` (#739 系 unit test) |
| SecurityError | `tests/LocalGameStorage.test.ts` (#739 系 unit test) |
| write failure preserves older readable snapshot | `tests/LocalGameStorage.test.ts` (#739 系 integration test) |

Save failure / corrupt JSON / unsupported schema / QuotaExceededError / SecurityError は #621/#739 系の unit/integration test として参照する（本 E2E のスコープ外）。

## Scope boundary

- 本ドキュメントは Playwright E2E 自動化による Gate 証跡である。
- `__E2E_SHORT_SORTIE__` fixture を使用（timeout terminal のみ）。
- victory / defeat terminal の確認は m2-combat-mvp.spec.ts でカバー済み。
- 手動 playtest（UX 評価）は manual-playtest-runbook.md に従う。

## Limitations

- QuotaExceededError path: E2E 未証明（#621/#739 unit test でカバー）
- write failure path: E2E 未証明（#621/#739 unit test でカバー）
- older readable snapshot preservation: #621/#739 unit test でカバー
- victory / defeat terminal での reward: m2-combat-mvp.spec.ts でカバー
- 武器強化 (weaponPower): M4 スコープ（本 E2E は weaponPower=1 固定）

## Origin and storage cautions

- production と PR preview は同一 origin を共有し得る
- same-origin caution: #885 で `loop-protocol.preview.pr-<pr-number>.mvp.save` へ key を分離済み
- E2E 専用 key `loop-protocol.e2e.<worker-scope>.mvp.save` は production key から分離されている
- E2E テストは production key を上書きしない（AC2）

## Follow-up routing

- `#733`: M3 parent の close 判定（人間 playtest 承認）
- `#622`: Save/Load 制約を維持した Pause / Checkpoint / Assist Suspend の方針（#733 gate 前提）
