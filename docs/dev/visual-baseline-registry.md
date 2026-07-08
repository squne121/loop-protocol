---
title: Visual Baseline Contract Registry
status: active
related_issue: 749
related: [681, 727, 747, 726, 732, 222]
---

# Visual Baseline Contract Registry（視覚契約台帳）

この文書は、E2E visual regression における「人間が承認した視覚仕様の固定点」を契約として
台帳化し、その更新ポリシーと CI 証跡パイプラインの運用ルールを定義する正本である。

baseline PNG は「正解画像」ではなく **人間が承認した視覚仕様の固定点** であり、設計変更に
紐づくレビュー対象の証跡として扱う。本台帳は「存在しない/暫定の visual contract を固定した
ことにしない」ことを主眼とする。

> スコープ: 本文書は registry schema・分類・更新ポリシー・CI 証跡配線の運用ルールを定義する。
> 個別の描画仕様変更や baseline PNG 自体の再生成（#747 の責務）・auto-update pipeline 構築
> （#681 で意図的に out of scope）は扱わない。

## 1. 前提（現行テスト実体）

registry は推測ではなく現行テスト実体に基づいて分類する。確認済みの実体は以下のとおり。

- `tests/e2e/m2-combat-mvp.spec.ts` で `toHaveScreenshot()` を使うのは **timeout overlay と
  running HUD の 2 件のみ**（`animations: 'disabled'`, `maxDiffPixels: 1`）。
- defeat overlay と HP label は `getImageData()` 系の pixel / predicate 検証であり、screenshot
  baseline PNG ではない。
- `.command-rail` / right rail / two-column shell / `.battle-stage` 外の normal-play controls を含む
  既存行動は、frozen 契約として固定しない。これらは `legacy-current` または
  `pending-baseline` にとどめ、`merged PR SHA` / `artifact digest` / `environment fingerprint` が
  全件確定した後のみ frozen 遷移候補とする。
- baseline PNG は `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/` 配下に存在する:
  `m2-timeout-overlay-baseline.png`, `m2-running-hud-baseline.png`、および **現行テストから
  参照されていない stale な `m2-defeat-overlay-baseline.png`**。
- `playwright.config.ts`: viewport `1280x720`、project `chromium`（`Desktop Chrome`）、
  `snapshotPathTemplate: '{testDir}/__screenshots__/{testFilePath}/{arg}{ext}'`。
  `deviceScaleFactor` / `colorScheme` / `reducedMotion` / `locale` / `timezoneId` は未指定
  （`Desktop Chrome` device の既定値に従う。`deviceScaleFactor` は既定 1）。

## 2. registry schema（台帳スキーマ）

各エントリは最低限以下の列を持つ。`kind` は検証手段の分類、`maturity` は仕様としての確定度を表す。

| 列 | 意味 |
|---|---|
| `id` | baseline / contract の識別子 |
| `kind` | `screenshot-baseline` / `pixel-contract` / `predicate-only` |
| `maturity` | `frozen` / `provisional` / `legacy-current` / `pending-baseline` / `predicate-only`（下表「maturity の定義」参照） |
| `artifact/test` | 対応する baseline PNG または test 実体 |
| `spec` | 対応する仕様 Issue / feature doc |
| `fixed contract` | 守るべき不変条件（必ず守る意味差分） |
| `mutable elements` | 固定しない要素（最終 UI 配置・装飾・将来の再設計など） |
| `tolerance` | screenshot baseline の許容差（`maxDiffPixels` 等）と理由 |
| `update condition` | 更新してよい条件 |
| `invalidated_by` | この契約を無効化 / 再生成し得る Issue |

### kind の定義

- `screenshot-baseline`: `toHaveScreenshot()` で baseline PNG と pixel 比較する契約。PNG が
  正本の証跡。
- `pixel-contract`: exact screenshot ではなく `getImageData()` 等で特定ピクセル特性（色支配性
  など）を意味検証する契約。baseline PNG を持たない。
- `predicate-only`: pixel 値そのものではなく述語（bounds 内 / NaN でない / 可読 等）のみを検証
  する契約。

### maturity の定義

- `frozen`: 人間が pixel / visual を仕様として固定済み。差分は仕様変更レビューを要する。
- `provisional`: 現状は回帰検知用。関連 Issue（例 #727）の再開時に破棄 / 再生成し得る。
- `legacy-current`: 既存実装で観測される legacy な UI を表現する。right rail / command rail /
  two-column 依存がある前提の状態を含み、frozen 条件を満たす前提で `pending` / `legacy` 運用する。
- `pending-baseline`: 先行登録された契約。artifact/test の committed 参照、active PASS claim、active CI
  suite がまだ未確定で、遷移待機中。
- `predicate-only`: exact screenshot ではなく意味検証のみ。pixel は未固定。

## 3. 契約エントリ

| id | kind | maturity | artifact/test | spec | fixed contract | mutable elements | tolerance | update condition | invalidated_by |
|---|---|---|---|---|---|---|---|---|---|
| timeout-overlay | screenshot-baseline | frozen | `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-timeout-overlay-baseline.png`（`m2-combat-mvp.spec.ts` の timeout overlay baseline test） | #732 / #681 / #747 | timeout は defeat ではない中立終了表示であること・背景 tint・可読性・整数段階表示 | 色味 / 最終配置は UI 再設計で変更可 | `maxDiffPixels: 1`（理由: CI Chromium + 固定 viewport 1280x720 + 決定論的 E2E モード前提でのみ妥当） | 意図した視覚仕様変更を人間がレビューし承認した場合のみ（§4 checklist 経由） | #727（HUD/layout 再設計） |
| running-hud | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-running-hud-baseline.png`（`m2-combat-mvp.spec.ts` の running HUD baseline test） | #681 / #726 / #727 / #1370 / #1375 / #1377 / #1380 | running HUD が描画されること・HULL/HP の小数露出がないこと・桁溢れがないこと | 色味 / 詳細配置 / right rail 依存は再設計まで可変 | `maxDiffPixels: 1`（理由: legacy-current。#727 再設計時に再評価する） | #727 再開時または #1370 / #1375 / #1377 / #1380 系の overlay rollout 進行時に破棄 / 再分類可 | #727 / #1370 / #1375 / #1377 / #1380（HUD/layout 再設計と overlay rollout） |
| defeat-overlay | pixel-contract | predicate-only | `getImageData` smoke（`m2-combat-mvp.spec.ts` の defeat overlay 赤支配ピクセル検証 / AC8） | #681 / #732 | defeat overlay が赤系・終端状態として識別可能であること | exact pixels は未固定。最終 layout / 色味は未確定 | N/A（screenshot baseline ではない） | predicate（赤支配）が壊れた場合のみテスト側を調整 | #727 |
| hp-label | predicate-only | predicate-only | HP label bounds smoke（`m2-combat-mvp.spec.ts` の HP label bounding box 検証 / AC5） | #726 / #727 | HP label が viewport 外 / NaN 表示にならない・bounds 内・可読であること | 最終 UI 表現 / 配置は未固定 | N/A（screenshot baseline ではない） | predicate（bounds / 可読）が壊れた場合のみテスト側を調整 | #727 |
| running-hud-paused | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1375 / #1377 | running HUD の停止状態でも command-rail / right rail / two-column shell / `.battle-stage` 外 controls への依存がないことを明示 | frozen 適用対象外。duration 等の固定は `durationMs` / `fixedDeltaMs` で判定可能な場合に限定 | pending: no PNG/test（active PASS claim 保留） | §4 の `maturity transition` を満たした時点で `legacy-current -> frozen` | #1370 / #1375 / #1377 |
| result-overlay-timeout | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1377 / #1375 | result overlay timeout の timeout 時間表現を、`elapsedTicks` 由来表示ではなく `durationMs` / `fixedDeltaMs` を優先する | 右寄り controls への依存や focus / inert / keyboard / dialog 条件は frozen 直前に検証 | pending: no PNG/test（active PASS claim 保留） | §4 の `maturity transition` を満たした時点で `legacy-current -> frozen` | #1380 / #1377 |
| final-no-command-rail | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1370 / #1377 | 最終結果画面が `command rail` 未依存でも意図読取できること。right rail / battle-stage 外依存は frozen 禁止条件 | #1370 / #1377 の影響条件を満たすまで固定化しない | pending: no PNG/test（active PASS claim 保留） | `merged PR SHA` と `artifact URL` / `artifact digest` / `environment fingerprint` が確定した時点で `legacy-current -> frozen` | #1380 / #1370 / #1377 |

### pending-baseline / legacy-current の遷移規則

`legacy-current` / `pending-baseline` から `frozen` へ変更する際は、以下を満たすこと。

- merged PR SHA、active test path、committed PNG path、right rail / command-rail / two-column 依存除外の
  レビュー記録がそろっていること。
- `artifact URL` と `artifact digest` は CI summary / CI evidence matrix から復元可能であること。
- deterministic fixture / freeze or mask 条件、`duration` / `timer` の源泉を `durationMs` / `fixedDeltaMs` / 
  `elapsedTicks` の優先順で定義すること（`elapsedTicks` だけの `fixed` は不可）。
- `expected / actual / diff` の review 記録（有無を含む）を残すこと。
- baseline transition の `old maturity`、`new maturity`、`transition reason`、`right-rail dependency`
  を明記すること。
- `legacy-current` / `pending-baseline` 行は `timeout-overlay` を除き、`active screenshot` と `PNG path`
  を持たせない。

AC 補助行（確認シグナル）:
predicate-only と frozen / provisional / legacy-current / pending-baseline は本台帳の全行で定義。
`command-rail` / right rail / two-column / battle-stage の依存は frozen 前提で許容しない。
`running-hud-paused` / `result-overlay-timeout` / `final-no-command-rail` は `pending-baseline` で `pending: no PNG/test` と明記。
`defeat-overlay` と `hp-label` は `predicate-only` のまま維持し、screenshot-baseline へ再分類しない。
`timeout-overlay` / `running-hud` / `defeat-overlay` / `hp-label` は既存列挙を維持し、判定行を分割更新しない。
`#1370` / `#1375` / `#1377` / `#1380` の連鎖影響を `spec`/`invalidated_by` に保持。
`merged PR SHA` / `artifact URL` / `artifact digest` / `environment fingerprint` が揃ったときにのみ遷移可。
`elapsedTicks` / `durationMs` / `fixedDeltaMs` の優先順を明記。
`@vitest/browser-playwright` と `vitest.visual.config.ts` は Component VRT の前提で、Component VRT と screenshot directory は別実装とする。
`active CI suites` と `check-visual-artifact-pipeline` / `CI summary` / `cross-validation` を同時に満たす。
`focus` / `inert` / `keyboard` / `dialog` の補助検証は frozen 代替とせず、#1373-#1376 行動系テストを前提化。
`old maturity` / `new maturity` / `transition reason` / `right-rail dependency` を review checklist で保存。
screenshot-baseline / pending-baseline / pending: no PNG/test / legacy-current / right rail / timeout-overlay の同居を明記。

### defeat-overlay の #681 契約 supersession（明示）

defeat-overlay は **#681 時点では screenshot-baseline 候補**として扱われ、`m2-defeat-overlay-baseline.png`
が生成された。しかし現在の `tests/e2e/m2-combat-mvp.spec.ts` には defeat-overlay に対する
`toHaveScreenshot()` 参照が存在せず、defeat-overlay の検証は `getImageData` による赤支配
ピクセル検証（pixel-contract）として実装されている。

- **再分類**: 本台帳（#749 / PR #760）は defeat-overlay を `pixel-contract` / `predicate-only`
  として再分類する。これは現行テスト実体に一致させる正当な整理である。
- **supersession**: この再分類は **#681 の「defeat overlay に screenshot baseline を導入する」
  該当 AC を supersede する**。今後 defeat-overlay の正本は #681 ではなく本台帳である。将来
  defeat-overlay を screenshot-baseline 化したい場合は、別 Issue で `toHaveScreenshot()` を
  追加した上で本台帳の kind/maturity を更新すること。

### stale baseline PNG の判断（`m2-defeat-overlay-baseline.png`）

上記 supersession の結果、`m2-defeat-overlay-baseline.png` は本台帳のどの screenshot-baseline
契約にも対応しない **stale な未参照ファイル** である。

- **判断**: `m2-defeat-overlay-baseline.png` は登録 baseline ではない。削除する。
- **削除 follow-up Issue（必須）**: 削除は follow-up Issue **#761** で実施する。`tests/e2e/__screenshots__/**`
  は本 Issue（#749）の Allowed Paths 外のため本 PR では削除しない。削除完了までの残置は
  本台帳に登録しないことで新たな frozen 契約を生まない扱いとする（#761 でファイル削除を完了する）。

## 4. baseline update policy（更新ポリシー）

### 自動更新の禁止

- **CI が PNG を生成して正とする運用（snapshot auto-update）を禁止する。** baseline PNG の
  追加 / 更新は必ず人間の commit / review を前提とする。
- CI ジョブで `--update-snapshots` 系のフラグを常用しない。意図しない退行をそのまま「正」に
  固定するリスクを避けるため。

### baseline 変更 PR の review checklist（PR review checklist for baseline changes）

baseline PNG を追加 / 更新する PR のレビューでは、以下を必ず確認する。

- [ ] **差分画像の確認**: `test-results` artifact 内の `*-actual.png` / `*-expected.png` /
      `*-diff.png`、または PR の差分プレビューで、視覚差分を目視確認した。
- [ ] **意図した仕様変更か退行固定化かの判断**: 差分が意図した視覚仕様変更（spec / Issue に
      紐づく）であることを確認した。意図しない退行を baseline として固定していない。
- [ ] **環境 fingerprint の確認**: baseline が生成された環境が CI 比較環境（runner OS / browser /
      viewport / deviceScaleFactor / Playwright version）と一致することを `$GITHUB_STEP_SUMMARY`
      の fingerprint で確認した（§5 参照）。
- [ ] **maturity の整合**: `frozen` 化する場合、対象が `provisional` / `predicate-only` で
      留めるべき設計 churn 中の領域（例 #727 deferred な HUD / HP label）でないことを確認した。
- [ ] **maturity transition の整合**: `old maturity`、`new maturity`、`transition reason`、`right-rail dependency` を
  PR 本文に明記し、右依存が除去済みであることを確認した。
- [ ] **evidence の整合**: `expected / actual / diff`（pass 時は N/A）、`merged PR SHA`、`active test path`、
  `artifact URL`、`artifact digest`、`environment fingerprint` を添付している。
- [ ] **tolerance の妥当性**: `maxDiffPixels` 等の許容差が registry の記載と一致し、その理由が
      現行 CI 環境で妥当であることを確認した。

## 5. CI 証跡パイプライン（artifact / summary 配線）

`.github/workflows/ci.yml` の e2e ジョブは以下の方針で visual regression 証跡を残す。

**配線は CI の常設ゲートとして検証する（退行防止）**: 構造検証は
`scripts/check-visual-artifact-pipeline.py`（YAML を構造解析。`scripts/check-visual-artifact-pipeline.sh`
は同 py を呼ぶ wrapper）で行い、`.github/workflows/ci.yml` の **`python-test` ジョブに
`uv run --locked python scripts/check-visual-artifact-pipeline.py` ステップとして常設**する。
これにより、後続 PR で `if: failure()` への差し戻しや retention 変更などの退行が発生した場合、
required check が fail して止まる。手元実行だけのゲートにしない。

検証スクリプトは以下を **hard fail**（範囲・存在のみではなく値の完全一致）で検査する:

- `uses` が許可 pin（`actions/upload-artifact@v6`）と完全一致。look-alike（例
  `actions/upload-artifact-malicious@v6`）は action 名の完全一致で弾く。
- `if` が `${{ !cancelled() }}` と完全一致（`always()` / `failure()` は不可）。
- `id` が `upload-playwright-report` / `upload-test-results`、`with.name` が
  `playwright-report` / `test-results`、`with.path` が `playwright-report/` / `test-results/`。
- `if-no-files-found == warn`、`retention-days == 30`。
- upload 後の summary step が `$GITHUB_STEP_SUMMARY` と両 upload step の
  `outputs.artifact-url`、および必須 fingerprint トークン（runner / node / Playwright / browser /
  project / viewport / deviceScaleFactor / snapshotPathTemplate / baseline path / animations）を含む。
- **fingerprint cross-validation（嘘防止）**: summary が echo する `viewport` /
  `snapshotPathTemplate` / `maxDiffPixels` を `playwright.config.ts` と
  `tests/e2e/m2-combat-mvp.spec.ts` の実値と照合する。config / spec を変更して summary を
  更新し忘れた（またはその逆）場合は fail する。これにより fingerprint が「人間向けメモ」では
  なく実際の比較条件を表す監査情報であることを保証する。

### artifact upload

- `playwright-report/` と `test-results/` を **成功 / 失敗いずれでも upload する**。
- 既定 condition は `if: ${{ !cancelled() }}` とする。`always()` は使わない。
  - `!cancelled()` を選ぶ理由: 成功 / 失敗いずれでも証跡を残したいが、キャンセル run まで保存
    する必要はないため。`always()` はキャンセル run や重大 failure 時にも実行され、hang
    リスクや不要な証跡保存を招き得る。
  - **例外**: `always()` を使う場合は「キャンセル run でも証跡を保存する必要がある」理由を本節
    に明記すること（現状は不要のため `!cancelled()` を採用）。
- 各 upload step は `id:` を持ち、後続 summary step から `outputs.artifact-url` を参照できる。
- `if-no-files-found: warn`、`retention-days: 30`（レビュー証跡として M2/M3 milestone review まで
  追跡できるよう、従来の 14 days から延長）。

### upload-artifact のバージョン方針

- `actions/upload-artifact` は **`@v6` に固定**する。理由: 現行 `.github/workflows/ci.yml` 内の
  既存 `upload-artifact` 利用（e2e の従来 step / `python-test` の artifact upload）がすべて `@v6`
  であり、リポジトリ全体の pin を統一するため。`outputs.artifact-url`（summary が依存する出力）は
  v4 以降で提供される公式出力であり `@v6` で利用可能。
- major bump（例 `@v7`）は CI 証跡基盤全体の方針変更として、本台帳のバージョン方針と検証
  スクリプトの許可 pin（`ALLOWED_UPLOAD_USES`）を同時に更新する別レビューで行う。検証スクリプト
  が許可 major を明示的に保持することで、暗黙のバージョン drift を防ぐ。

### summary step（環境 fingerprint と artifact link）

- summary step は **artifact upload step の後** に置く。`outputs.artifact-url` は upload 後にしか
  得られないため（`$GITHUB_STEP_SUMMARY` は step ごとのファイルで、後続 step から過去 summary を
  書き換えられない）。
- summary step は `$GITHUB_STEP_SUMMARY` に以下の環境 fingerprint と artifact link を記録する:
  runner / OS（`RUNNER_OS` / `RUNNER_ARCH` / image version）・Node version・Playwright version・
  browser（chromium）・project（`chromium` / `Desktop Chrome`）・viewport（1280x720）・
  deviceScaleFactor（1）・`snapshotPathTemplate`・baseline path・screenshot options
  （`animations` / `maxDiffPixels`）・`outputs.artifact-url`。
- actual / expected / diff は **visual mismatch が発生した場合のみ** `test-results` artifact 内
  の参照として記録し、存在しない場合（pass 時）は **N/A と明記する**。常に link を要求しない
  （pass run では actual/expected/diff が存在しないため）。

### component VRT / active suite 依存

- `@vitest/browser-playwright`、`vitest.visual.config.ts`、対象 overlay module の 3 要件が揃うまでは、
  Component VRT は未導入として扱い、上記 `running-hud-paused` / `result-overlay-timeout` /
  `final-no-command-rail` は `pending-baseline` 維持とする。
- `active CI suites` と `check-visual-artifact-pipeline` の cross-validation が揃っていない場合、`legacy-current` / `pending-baseline`
  からの frozen 昇格は保留する。frozen 昇格時には `merged PR SHA`、`CI summary`、`artifact path` を再確認する。
- focus / inert / keyboard / dialog の振る舞いを理由に frozen 昇格を代替しない。#1373-#1376 の
  behavior test（フォーカス移動 / inert 化 / キーボード遷移 / dialog 開閉）が完了していることを
  先行条件として参照する。

## 6. 関連

- #681（`toHaveScreenshot` baseline 追加 / auto-update pipeline は out of scope）
- #727（Canvas HUD 集約・layout 再設計 / deferred。本台帳の `provisional` / `predicate-only`
  契約を無効化し得る）
- #747（`m2-timeout-overlay-baseline.png` の CI レンダリング再生成 hotfix）
- #726（HUD 整数段階表示）/ #732（timeout 中立 terminal）/ #222（PR テンプレート Runtime
  Verification Evidence）
