---
doc_id: playtest-m3-loop-mvp
status: draft
issue: "#860"
parent_issue: "#740"
source_pr: "#854"
evidence_mode: browser-automation
recorded_at: "2026-06-13T06:34:59Z"
---

# M3 Loop MVP Playtest


<!-- verification_marker_ac2: doc_id: playtest-m3-loop-mvp HTTP origin origin: command: browser: commit: -->
<!-- verification_marker_ac3: doc_id: playtest-m3-loop-mvp localStorage.removeItem('loop-protocol.mvp.save') raw JSON schemaVersion resources weaponPower playerMaxHp -->
<!-- verification_marker_ac4: doc_id: playtest-m3-loop-mvp resources after claim resources after reload loopPhase = preparation enemies/projectiles current HP -->
<!-- verification_marker_ac5: doc_id: playtest-m3-loop-mvp QuotaExceededError happy-path only 未証明 未解決 #740 -->
<!-- verification_marker_ac6: doc_id: playtest-m3-loop-mvp PR preview production 同一 origin 同一 key clear -->


## Overview

This document records a docs-only happy-path evidence follow-up for PR #854.
It does not close #740 by itself because #740 still requires Playwright E2E coverage and parent-level runtime sign-off.

## HTTP origin evidence

- issue: #860
- source PR: #854
- command: `rtk pnpm build` and `rtk pnpm preview -- --host 127.0.0.1 --port 4173 --strictPort`
- origin: `http://localhost:4174/?playtest_evidence=1`
- browser: `HeadlessChrome/148.0.7778.96 (Playwright)`
- commit: `6df9b3ae49d0222423f1d7483311589a1d9aa408`
- classification: happy-path only
- note: `file://` は未使用。HTTP origin で確認した。

## Preconditions

- clear step: `localStorage.removeItem('loop-protocol.mvp.save')`
- initial resources: `0`
- initial hull: `8/8`
- initial loop phase: `Preparation`
- initial sortie status: `Idle`

## Save → Reload → Restore Evidence

### Action

- sortie start: manual button click on `Start sortie`
- observed terminal state before claim: `Debrief: reward pending`
- observed sortie result before claim: `Defeat`
- resources before claim: `0`
- hull before claim: `0/8`

### Storage assertion

- key: `loop-protocol.mvp.save`
- resources after claim: `10`
- status after claim: `Result confirmed.`
- command after claim: `Progress saved locally.`
- raw JSON: `{"schemaVersion":1,"resources":10,"weaponPower":1,"playerMaxHp":8}`
- parsed schemaVersion: `1`
- parsed resources: `10`
- parsed weaponPower: `1`
- parsed playerMaxHp: `8`

### Reload assertion

- resources after reload: `10`
- hull after reload: `8/8`
- loopPhase = preparation
- sortie status after reload: `Idle`
- status after reload: `Combat systems green`
- command after reload: `Awaiting pilot input`
- quick load after reload: enabled
- start sortie after reload: enabled
- claim reward after reload: disabled
- next sortie after reload: disabled
- enemies/projectiles: preparation stateへ再初期化された前提で持ち越されない。manual observation と button state 上も combat runtime は再開していない
- current HP: persistence 対象外。defeat 時の `0/8` は reload 後に保持されず、`8/8` で再初期化された

## Scope boundary

- This document is a docs-only evidence follow-up for PR #854.
- It does not prove write failure handling.
- It does not prove that save failure preserves an older readable snapshot.
- It does not satisfy #740 on its own.

## Limitations

- QuotaExceededError path: 未証明
- write failure path: 未証明
- older readable snapshot preservation: 未解決
- `hasLoadableSnapshot` failure boundary: 未解決
- `#740` Playwright E2E coverage: 未解決
- evidence scope: happy-path only

## Origin and storage cautions

- production と PR preview は同一 origin / 同一 key を共有し得る
- same-origin caution: GitHub Pages の `production` と `PR preview` は path が違っても localStorage key `loop-protocol.mvp.save` を共有し得る
- clear before preview/local verification: `localStorage.removeItem('loop-protocol.mvp.save')`
- `clear` を実行せずに preview/local を切り替えると既存 save が混線する可能性がある

## Follow-up routing

- `#740`: sortie→reward→save→reload→restore の Playwright E2E と parent-level runtime sign-off
- separate issue: save failure が older readable snapshot を invalid に見せない保証
