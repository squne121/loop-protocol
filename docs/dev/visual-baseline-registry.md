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
| `maturity` | `frozen` / `provisional` / `predicate-only`（下表「maturity の定義」参照） |
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
- `predicate-only`: exact screenshot ではなく意味検証のみ。pixel は未固定。

## 3. 契約エントリ

| id | kind | maturity | artifact/test | spec | fixed contract | mutable elements | tolerance | update condition | invalidated_by |
|---|---|---|---|---|---|---|---|---|---|
| timeout-overlay | screenshot-baseline | frozen | `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-timeout-overlay-baseline.png`（`m2-combat-mvp.spec.ts` の timeout overlay baseline test） | #732 / #681 / #747 | timeout は defeat ではない中立終了表示であること・背景 tint・可読性・整数段階表示 | 色味 / 最終配置は UI 再設計で変更可 | `maxDiffPixels: 1`（理由: CI Chromium + 固定 viewport 1280x720 + 決定論的 E2E モード前提でのみ妥当） | 意図した視覚仕様変更を人間がレビューし承認した場合のみ（§4 checklist 経由） | #727（HUD/layout 再設計） |
| running-hud | screenshot-baseline | provisional | `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-running-hud-baseline.png`（`m2-combat-mvp.spec.ts` の running HUD baseline test） | #681 / #726 / #727 | running HUD が描画されること・HULL/HP の小数露出がないこと・桁溢れがないこと | HUD 詳細配置は #727 再開まで provisional（最終 layout 未確定） | `maxDiffPixels: 1`（理由: provisional。#727 再設計時に再評価する） | #727 再開時に破棄 / 再生成可。それまでは回帰検知のため意図変更時のみ更新 | #727（HUD/layout 再設計） |
| defeat-overlay | pixel-contract | predicate-only | `getImageData` smoke（`m2-combat-mvp.spec.ts` の defeat overlay 赤支配ピクセル検証 / AC8） | #681 / #732 | defeat overlay が赤系・終端状態として識別可能であること | exact pixels は未固定。最終 layout / 色味は未確定 | N/A（screenshot baseline ではない） | predicate（赤支配）が壊れた場合のみテスト側を調整 | #727 |
| hp-label | predicate-only | predicate-only | HP label bounds smoke（`m2-combat-mvp.spec.ts` の HP label bounding box 検証 / AC5） | #726 / #727 | HP label が viewport 外 / NaN 表示にならない・bounds 内・可読であること | 最終 UI 表現 / 配置は未固定 | N/A（screenshot baseline ではない） | predicate（bounds / 可読）が壊れた場合のみテスト側を調整 | #727 |

### stale baseline PNG の判断（`m2-defeat-overlay-baseline.png`）

現行 spec から参照されていない `m2-defeat-overlay-baseline.png` は、本台帳のどの
screenshot-baseline 契約にも対応しない **stale な未参照ファイル** である。defeat-overlay は
`pixel-contract`（`getImageData` による赤支配検証）であり、exact screenshot baseline を
持たない。

- **判断**: `m2-defeat-overlay-baseline.png` は登録 baseline ではない。削除を推奨する。
- **削除の取り扱い**: `tests/e2e/__screenshots__/**` は本 Issue（#749）の Allowed Paths 外の
  ため、本 PR では削除しない。削除は follow-up Issue で行う（registry には登録しないため、
  残置していても新たな frozen 契約を生まない）。

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
- [ ] **tolerance の妥当性**: `maxDiffPixels` 等の許容差が registry の記載と一致し、その理由が
      現行 CI 環境で妥当であることを確認した。

## 5. CI 証跡パイプライン（artifact / summary 配線）

`.github/workflows/ci.yml` の e2e ジョブは以下の方針で visual regression 証跡を残す。配線の
構造検証は `scripts/check-visual-artifact-pipeline.sh`（内部で
`scripts/check-visual-artifact-pipeline.py` が YAML を構造解析）で行う。

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

## 6. 関連

- #681（`toHaveScreenshot` baseline 追加 / auto-update pipeline は out of scope）
- #727（Canvas HUD 集約・layout 再設計 / deferred。本台帳の `provisional` / `predicate-only`
  契約を無効化し得る）
- #747（`m2-timeout-overlay-baseline.png` の CI レンダリング再生成 hotfix）
- #726（HUD 整数段階表示）/ #732（timeout 中立 terminal）/ #222（PR テンプレート Runtime
  Verification Evidence）
