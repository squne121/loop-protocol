---
title: 視覚 Baseline Contract Registry
description: 視覚 baseline 契約台帳の正本であり、基準画像と検証条件を日本語で説明する文書
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
- `tests/e2e/visual-overlay.spec.ts`（primary VRT gate。#1386）の実行コマンドは 2 種類ある
  （PR #1721 review fix, P1 #4）。
  - `pnpm test:vrt`: ローカル用。`VITE_E2E_MODE=true pnpm build` で hermetic に再 build してから
    実行する（`import.meta.env.VITE_E2E_MODE` は Vite の build 時静的置換のため、`webServer.command`
    への env 注入だけでは既存 `dist` に対して手遅れ）。
  - `pnpm test:vrt:e2e`: CI 用。**「直前の CI ステップで `VITE_E2E_MODE=true` build 済みの `dist`
    が存在する」ことを precondition とする**。ローカルで単体実行する場合は先に
    `VITE_E2E_MODE=true pnpm build` を実行すること。
  - 両コマンドとも `LOOP_VRT_LANE=true` を設定し、`playwright.config.ts` の `webServer.reuseExistingServer`
    を強制的に `false` にする。別 worktree / 別 commit / E2E モード無しで build された preview server
    が 4173 番ポートに残っていても、それを再利用せず必ず新しい preview server を起動する。
  - `tests/e2e/visual.freeze.css` は `[data-battle-ui-root]` 配下の `font-family` を generic family
    （`sans-serif`）に固定する。`src/style.css` の `--sans` / `--mono` は named font
    （`Segoe UI Variable` 等）の fallback chain のため、named font の有無がホストごとに異なると
    同じテキストでも折り返しが変わり得る（font metrics drift）。

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
  正本の証跡。ただし `maturity` が `pending-baseline` の行は将来の screenshot 契約名を予約する
  予定行であり、`pending: no PNG/test` として PNG / test / PASS claim を持たない。
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
| running-hud | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-running-hud-baseline.png`（`m2-combat-mvp.spec.ts` の running HUD baseline test。Issue #1375 で capture root を `[data-field="sortie-status"]` 単一 field から `[data-combat-hud]` パネル全体に変更し、Hull/Kills/Elapsed/Weapon/Assist status の各 field は mask 済み） | #681 / #726 / #727 / #1370 / #1375 / #1377 / #1380 | running HUD が描画されること・HULL/HP の小数露出がないこと・桁溢れがないこと・combat HUD が Hull/Kills/Elapsed/Weapon/Assist/Pause のみで構成されること（#1375 AC2） | 色味 / 詳細配置 / right rail 依存は再設計まで可変 | `maxDiffPixels: 150`（Issue #1375 で `maxDiffPixelRatio: 0.08` から変更。理由: capture root を単一 field から複数 field を含むパネル全体へ拡張したため、絶対ピクセル数の許容差に統一した。masked field 以外のパネル chrome/ラベルのみを比較） | #727 再開時または #1370 / #1375 / #1377 / #1380 系の overlay rollout 進行時に破棄 / 再分類可 | #727 / #1370 / #1375 / #1377 / #1380（HUD/layout 再設計と overlay rollout） |
| running-hud-overlay-legacy-current | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/visual-overlay.spec.ts/vrt-running-hud-overlay.png`（`tests/e2e/visual-overlay.spec.ts` の `[data-battle-ui-root]` DOM overlay baseline test） | #1386 / #1380 / #1370 / #1374 | `[data-battle-ui-root]` DOM overlay 全体（HUD 各 field を含む）が描画されること。`running-hud`（`m2-combat-mvp.spec.ts` の単一 field baseline）とは別の独立した baseline であり、両者は衝突しない | 色味 / 詳細配置 / right rail・command-rail 依存は再設計まで可変。将来の overlay 再設計で `frozen` 化するまでは legacy-current のまま | `maxDiffPixels: 100`（絶対ピクセル数。理由: capture root が canvas mask を含み全体の過半を占めるため `maxDiffPixelRatio` は不採用。非mask領域のfont-rasterizationノイズに対しこのworktree環境で複数回実測し PASS した最小幅に margin を加えた値。`tests/e2e/visual.freeze.css` で capture root 配下の font-family を generic family（`sans-serif`）に固定し、host font fallback chain 依存を除去済み） | §4 の `maturity transition` を満たした時点で `legacy-current -> frozen`（#1375/#1376/#1377 マージ後） | #1375 / #1376 / #1377 / #1380（overlay UI 実装マージで破棄・再生成対象） |
| defeat-overlay | pixel-contract | predicate-only | `getImageData` smoke（`m2-combat-mvp.spec.ts` の defeat overlay 赤支配ピクセル検証 / AC8） | #681 / #732 | defeat overlay が赤系・終端状態として識別可能であること | exact pixels は未固定。最終 layout / 色味は未確定 | N/A（screenshot baseline ではない） | predicate（赤支配）が壊れた場合のみテスト側を調整 | #727 |
| hp-label | predicate-only | predicate-only | HP label bounds smoke（`m2-combat-mvp.spec.ts` の HP label bounding box 検証 / AC5） | #726 / #727 | HP label が viewport 外 / NaN 表示にならない・bounds 内・可読であること | 最終 UI 表現 / 配置は未固定 | N/A（screenshot baseline ではない） | predicate（bounds / 可読）が壊れた場合のみテスト側を調整 | #727 |
| running-hud-paused | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/visual-overlay.spec.ts/vrt-running-hud-paused-overlay.png`（`tests/e2e/visual-overlay.spec.ts` の `[data-battle-ui-root]` DOM overlay baseline test。Issue #1376 AC12 で active 昇格） | #1380 / #1375 / #1376 / #1377 / #1391 | running HUD の停止状態でも command-rail / right rail / two-column shell / `.battle-stage` 外 controls への依存がないことを明示し、pause dialog（`role="dialog"`、Resume 初期 focus、Canvas/combat HUD inert 化）が描画されること | 色味 / 詳細配置は再設計まで可変。将来の overlay 再設計で `frozen` 化するまでは legacy-current のまま | `maxDiffPixels: 100`（絶対ピクセル数。`running-hud-overlay-legacy-current` と同じ capture root・同じ理由で採用。ローカル環境で複数回実測し PASS した） | §4 の `maturity transition` を満たした時点で `legacy-current -> frozen`（#1377 マージ後） | #1370 / #1375 / #1376 / #1377 / #1380 |
| result-overlay-timeout | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/visual-overlay.spec.ts/vrt-result-timeout-overlay.png`（`tests/e2e/visual-overlay.spec.ts` の `[data-battle-ui-root]` DOM overlay baseline test。Issue #1376 AC12 で active 昇格） | #1380 / #1376 / #1377 / #1392 | result overlay timeout の timeout 時間表現を `durationMs` / `fixedDeltaMs`（`RewardSystem.calculate()` の戻り値経由）から構築し、result dialog（`role="dialog"`、`tabindex="-1"` heading 初期 focus、Return to hangar 厳密1件）が描画されること | 色味 / 詳細配置は再設計まで可変。将来の overlay 再設計で `frozen` 化するまでは legacy-current のまま | `maxDiffPixels: 100`（絶対ピクセル数。`running-hud-overlay-legacy-current` と同じ capture root・同じ理由で採用。ローカル環境で複数回実測し PASS した） | §4 の `maturity transition` を満たした時点で `legacy-current -> frozen`（#1377 マージ後） | #1380 / #1376 / #1377 |
| final-no-command-rail | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1370 / #1377 | 最終結果画面が `command rail` 未依存でも意図読取できること。right rail / battle-stage 外依存は frozen 禁止条件 | #1370 / #1377 の影響条件を満たすまで固定化しない | pending: no PNG/test（active PASS claim 保留） | `merged PR SHA` と `artifact URL` / `artifact digest` / `environment fingerprint` が確定した時点で `legacy-current -> frozen` | #1380 / #1370 / #1377 |
| combat-hud-running (component VRT) | screenshot-baseline | provisional | `tests/component/__screenshots__/combat-hud-running.vrt.test.ts/`（`tests/component/combat-hud-running.vrt.test.ts`。Vitest Browser Mode、Playwright E2E VRT とは別の baseline root） | #1389 / #1380 / #1370 | `createHudController()` の production DOM を `src/style.css` 適用済みで mount し、`[data-combat-hud]` のみを撮影すること（full page / Canvas / legacy result surface / command rail は撮影しない） | 色味 / 詳細配置は UI 再設計まで可変。`combat-hud-running` 以外のシナリオは本 Issue の Out of Scope | `allowedMismatchedPixelRatio: 0.02`（Vitest Browser Mode `toMatchScreenshot()` の comparator。理由: `running-hud`/`running-hud-overlay-legacy-current` と同じ combat HUD 表示だが独立した capture root であり、font-rasterization ノイズに対する余裕として比率指定を採用） | component VRT は non-required/report-only（`component-vrt-report` CI job）のままである限り maturity 遷移条件は適用しない。required gate 化する場合は別 Issue で本行を frozen 遷移条件つきに更新する | #1380（VRT rollout tracker） |

### pending-baseline / legacy-current の遷移規則

`legacy-current` / `pending-baseline` から `frozen` へ変更する際は、以下を満たすこと。

- merged PR SHA、active test path、committed PNG path、right rail / command-rail / two-column 依存除外の
  レビュー記録がそろっていること。
- `artifact URL` / `artifact digest` は CI summary から復元可能であること。`artifact digest` は
  GitHub `upload-artifact` の公式 `outputs.artifact-digest` を CI summary が記録し、
  `check-visual-artifact-pipeline.py` が capture 単位で cross-validate する（#1387 / PR #1813
  review fix で配線・検証を実装済み）。
- deterministic fixture / freeze or mask 条件、`duration` / `timer` の源泉を `durationMs` / `fixedDeltaMs` / 
  `elapsedTicks` の優先順で定義すること（`elapsedTicks` だけの `fixed` は不可）。
- `expected / actual / diff` の review 記録（有無を含む）を残すこと。
- baseline transition の `old maturity`、`new maturity`、`transition reason`、`right-rail dependency`
  を明記すること。
- `pending-baseline` 行には `active screenshot` と `PNG path` を持たせない。`legacy-current` 行は
  既存 active screenshot を参照してよいが、その証跡だけで frozen 昇格済みとは扱わない。

frozen 昇格レビューでは、表の行とは別に以下の補助情報を PR 本文へ残す。

- `capture root`: screenshot locator root。原則 `.battle-stage` descendant とし、full page / `.app-shell`
  は理由付き例外として扱う。
- `determinism controls`: `stylePath` / mask / freeze CSS / fixture selector / volatile text handling。
- `evidence status`: `pending` / `active-pass` / `active-fail` / `retired`。

AC 補助行（確認シグナル）:
台帳の分類確認として、`predicate-only` と `frozen` / `provisional` / `legacy-current` / `pending-baseline` は全行で定義する。
右 rail 依存の確認として、`command-rail` / `right rail` / `two-column` / `battle-stage` の依存は frozen 前提で許容しない。
予約行の確認として、`final-no-command-rail` は `pending-baseline` で `pending: no PNG/test` と明記する（`running-hud-paused` / `result-overlay-timeout` は Issue #1376 AC12 で `legacy-current` へ昇格済み。下記「running-hud-paused / result-overlay-timeout の #1376 baseline 更新（明示）」参照）。
既存 predicate の確認として、`defeat-overlay` と `hp-label` は `predicate-only` のまま維持し、`screenshot-baseline` へ再分類しない。
既存行維持の確認として、`timeout-overlay` / `running-hud` / `defeat-overlay` / `hp-label` は既存列挙を維持し、判定行を分割更新しない。
親子 Issue 連鎖の確認として、`#1370` / `#1375` / `#1377` / `#1380` の影響を `spec` / `invalidated_by` に保持する。
昇格証跡の確認として、`merged PR SHA` / `artifact URL` / `artifact digest` / `environment fingerprint` が揃ったときにのみ遷移可とする。
時間表示の確認として、`elapsedTicks` / `durationMs` / `fixedDeltaMs` の優先順を明記する。
Component VRT の確認として、`@vitest/browser-playwright` と `vitest.visual.config.ts` は前提条件であり、`screenshot directory` は別実装とする。
CI 証跡の確認として、`active CI suites` と `check-visual-artifact-pipeline` / `CI summary` / `cross-validation` を同時に満たす。
行動検証の確認として、`focus` / `inert` / `keyboard` / `dialog` の補助検証は frozen 代替とせず、#1373-#1376 行動系テストを前提化する。
レビュー記録の確認として、`old maturity` / `new maturity` / `transition reason` / `right-rail dependency` を checklist で保存する。
semantic check の確認として、`screenshot-baseline` / `pending-baseline` / `pending: no PNG/test` / `legacy-current` / `right rail` / `timeout-overlay` の同居を明記する。

### registry semantic check（台帳の意味検証）

AC14 の検証では、単語の同居確認だけでなく、台帳表の行整合を one-shot check で確認する。

```bash
python3 - <<'PY'
from pathlib import Path

text = Path("docs/dev/visual-baseline-registry.md").read_text()
rows = [
    line for line in text.splitlines()
    if line.startswith("| ") and not line.startswith("|---")
]
# Issue #1376 AC12: running-hud-paused / result-overlay-timeout were
# promoted pending-baseline -> legacy-current (real committed PNG + active
# test). final-no-command-rail remains the sole pending-baseline reservation
# row (#1377, Out of Scope for #1376).
required_pending = {
    "final-no-command-rail",
}
required_promoted_active = {
    "running-hud-paused",
    "result-overlay-timeout",
}

def cells(row):
    return [c.strip() for c in row.strip("|").split("|")]

by_id = {}
for row in rows:
    cols = cells(row)
    if len(cols) >= 10 and cols[0] != "id":
        by_id[cols[0]] = cols

for rid in required_pending:
    assert rid in by_id, f"missing pending row: {rid}"
    row = by_id[rid]
    assert row[2] == "pending-baseline", f"{rid}: maturity must be pending-baseline"
    assert "pending: no PNG/test" in row[3], f"{rid}: must not point to committed PNG/test"
    assert "__screenshots__" not in row[3] and ".png" not in row[3], f"{rid}: fake PNG path"

for rid in required_promoted_active:
    assert rid in by_id, f"missing promoted row: {rid}"
    row = by_id[rid]
    assert row[2] == "legacy-current", f"{rid}: maturity must be legacy-current after AC12 promotion"
    assert "__screenshots__" in row[3] and ".png" in row[3], f"{rid}: must point to a committed PNG"
    assert "pending: no PNG/test" not in row[3], f"{rid}: must not still claim pending"

for rid in ["timeout-overlay", "running-hud", "defeat-overlay", "hp-label"]:
    assert rid in by_id, f"existing row missing: {rid}"

for rid, row in by_id.items():
    joined = " ".join(row)
    if any(token in joined for token in ["command-rail", "right rail", "two-column", "battle-stage 外"]):
        assert row[2] != "frozen" or rid == "timeout-overlay", (
            f"{rid}: right-rail dependent row is frozen"
        )

print("VISUAL_BASELINE_REGISTRY_SEMANTIC_CHECK_V1 status: pass")
PY
```

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

### running-hud-overlay-legacy-current の #1374 baseline 更新（明示）

Issue #1374（title / preparation を phase screen overlay に移す）は AC3 の要求どおり、
`running` phase で title / preparation の大パネルを `hidden` + `inert` にし、tab order
から除外する実装を追加した。この実装により `[data-battle-ui-root]` の running-hud
capture 内で表示される要素構成が変化し、`running-hud-overlay-legacy-current` の
既存 baseline（`vrt-running-hud-overlay.png`）との間に `maxDiffPixels: 100` を超える
差分（約 2%、12000px 台）が生じた。

- **判断根拠**: この差分は意図しない退行ではなく、AC3 が明示的に要求する仕様変更
  （running 中の title/preparation 大パネルの hidden/inert 化）の意図した副作用である
  （§4 checklist の「意図した仕様変更か退行固定化かの判断」に基づく確認）。
- **Scope Delta**: `tests/e2e/__screenshots__/`（本 baseline PNG）と本ファイル
  （`docs/dev/visual-baseline-registry.md`）は Issue #1374 契約の元の Allowed Paths に
  含まれていなかったため、本更新は Issue #1374 に対する Scope Delta として Allowed Paths を
  拡張した上で行う（人間承認: Issue #1374 実装ループ内, PR #1815 iteration 3 対応）。
- **evidence**: PR #1815（`worktree-issue-1374-phase-screen-overlay`）。ローカル worktree 環境で
  一度 `playwright test tests/e2e/visual-overlay.spec.ts --update-snapshots` により baseline を
  再生成したが、CI（GitHub Actions ubuntu runner）で実行した際に font-rasterization 差（ローカル
  Linux 環境と CI runner 間のフォント fallback 差）に起因すると見られる追加 diff（約 11000px 台）
  が再発した。そのため、CI 実行結果の `test-results` artifact（run id `30265197189`,
  job id `89974089069`（iteration 6, HudController の running 最小 HUD 再構成後の再取得））から `vrt-running-hud-overlay-actual.png` を取得し、これを最終 baseline
  として採用した（CI 環境自身が描画した画像を人間承認済みの Scope Delta commit として反映する運用。
  CI が自動で baseline を書き換えるわけではない — §4「自動更新の禁止」に抵触しない）。
- **maturity**: 変更なし（`legacy-current` のまま）。`frozen` 化条件（#1375/#1376/#1377
  マージ後）は本更新の対象外。
- **environment fingerprint**: 最終的に採用した baseline は CI 実行環境（GitHub Actions ubuntu
  runner, Chromium, Playwright config 既定 viewport, `tests/e2e/visual.freeze.css` の generic
  `sans-serif` 固定込み, run id `30265197189`）で生成された画像そのものである。以降の CI 再実行
  （同一コミット・同一 runner image 前提）でこの baseline との一致が期待される。

### running-hud / running-hud-overlay-legacy-current の #1375 baseline 更新（明示）

Issue #1375（running 用 combat HUD を battle-stage 内 overlay に再配置する）は AC1/AC2 の
要求どおり、`data-combat-hud`（running のみ表示）と `data-legacy-result-surface`（running
以外で表示）の 2 ルート構成へ `HudController` を再設計した。この実装により running phase の
HUD 構成要素（Hull/Kills/Elapsed/Weapon/Assist/Pause のみの compact panel）が変化し、
以下 2 件の既存 baseline との間に許容差を超える差分が生じた。

- **running-hud**（`m2-running-hud-baseline.png`）: capture root を `[data-field="sortie-status"]`
  単一 field から `[data-combat-hud]` パネル全体へ変更（旧 field は running 中に非表示となる
  legacy surface 側へ移動したため）。volatile な数値/status field は mask 済み。
- **running-hud-overlay-legacy-current**（`vrt-running-hud-overlay.png`）: `[data-battle-ui-root]`
  capture 内の running-time HUD 構成要素が変化（旧: Hull/Shots/Cooldown/Sortie/Wingmates/Pilot
  updates/Pause の複数 panel、新: compact combat HUD 単一 panel）。

- **判断根拠**: いずれの差分も意図しない退行ではなく、AC1/AC2 が明示的に要求する仕様変更
  （running 中の HUD を単一の compact player-facing surface に絞り込む）の意図した副作用で
  ある（§4 checklist の「意図した仕様変更か退行固定化かの判断」に基づく確認）。
- **evidence**: PR（`worktree-issue-1375-combat-hud-overlay`）。ローカル worktree 環境
  （Linux, Playwright chromium, `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み）
  で `playwright test --update-snapshots` により baseline を再生成した。#1374 の前例
  （本ファイル上記セクション）で観測された CI runner とのフォント fallback 差に起因する
  追加 diff が本 PR の CI 実行（run id `30693615617`, job id `91352588428`, e2e job,
  head sha `b328589b5acd25b2df688b8704d0d8da7e428a95`）で 9 件（`m2-running-hud-baseline` /
  `title-menu` / `load-menu-empty` / `load-menu-available` / `load-menu-failure` /
  `preparation-default` / `preparation-upgrade-available` / `running-minimal-hud` /
  `vrt-running-hud-overlay`）再発したため、CI 実行結果の `test-results` artifact
  （artifact id `8816541152`）から該当する `*-actual.png` を取得し、それを最終 baseline
  として採用した（CI 環境自身が描画した画像を人間承認済みの Scope Delta commit として
  反映する運用。CI が自動で baseline を書き換えるわけではない — §4「自動更新の禁止」に
  抵触しない）。差分は目視確認の上、font-rasterization / 環境差、または AC1/AC2 が要求する
  意図した仕様変更の副作用であり、意図しない退行の固定化ではないことを確認した
  （§4 checklist 適用）。
- **maturity**: 変更なし（`running-hud` は `legacy-current`、`running-hud-overlay-legacy-current`
  も `legacy-current` のまま）。`frozen` 化条件（#1376/#1377 マージ後）は本更新の対象外。
- **tolerance**: `running-hud` は `maxDiffPixelRatio: 0.08` から `maxDiffPixels: 150` へ変更
  （capture root が単一 field から複数 field を含むパネルへ拡張されたため）。
  `running-hud-overlay-legacy-current` の `maxDiffPixels: 100` は変更なし。
- **environment fingerprint**: 最終的に採用した baseline（上記 9 件）は CI 実行環境
  （GitHub Actions ubuntu runner, Chromium, Playwright config 既定 viewport,
  `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み, run id `30693615617`,
  job id `91352588428`）で生成された画像そのものである。以降の CI 再実行
  （同一コミット・同一 runner image 前提）でこの baseline との一致が期待される
  （`title-menu` / `load-menu-*` / `preparation-*` / `running-minimal-hud` は
  `phase-screens.spec.ts` の既存 baseline であり、本 Issue の AC1/AC2 実装による
  combat HUD 再配置に伴う周辺レイアウト・フォント差分の再取得として扱う）。

### running-hud 系 baseline の PR #1925 review 対応更新（明示）

PR #1925（Issue #1375）は owner の実機プレイテスト（Windows/Chrome, viewport 1437x1365,
devicePixelRatio 約 0.667）を受けた REQUEST_CHANGES で、combat HUD が Canvas ではなく
`battle-stage` 全体（header 含む）を containing block としていた defect（P0-1）と、それに
連動する二重 clipping boundary（P0-2）を修正した。この修正で以下の構造変更が入った。

- `src/main.ts`: `.battle-stage` 直下に `.battle-stage__header` と並列だった
  `.battle-stage__canvas` / `.battle-ui-layer` を、新設した `.battle-stage__viewport`
  （header の構造的な兄弟要素）の内側に移動した。
- `src/style.css`: `.battle-stage__viewport { position: relative; overflow: hidden }` を
  HUD overlay の唯一の containing block / clip boundary にし、`.battle-stage` 自身は
  `width: fit-content; max-width: min(960px, 100%)` で実際に描画される Canvas 幅（
  `src/main.ts` の `resizeArena()` が `state.arena.width` を 960px 上限でクランプしている）
  に一致するよう縮小した。これにより `[data-battle-ui-root]` / `[data-combat-hud]` の
  capture 領域が、従来の「grid column いっぱいに引き伸ばされた矩形（例: 1230x606）」から
  「実際の Canvas 矩形に一致する矩形（例: 900x507）」へ変化した。

この結果、以下の既存 baseline との間に許容差を超える差分が生じた。

- `running-hud`（`m2-running-hud-baseline.png`）
- `running-hud-overlay-legacy-current`（`vrt-running-hud-overlay.png`）
- `title-menu` / `load-menu-empty` / `load-menu-available` / `load-menu-failure` /
  `preparation-default` / `preparation-upgrade-available` / `running-minimal-hud`
  （`phase-screens.spec.ts`、いずれも `[data-battle-ui-root]` capture）
- `m2-timeout-overlay-baseline.png`（`canvas.battle-stage__canvas` 単体 capture。
  `maxDiffPixels: 1` という極めて狭い許容差のため、Canvas 自体のビットマップ内容は
  不変だが、`.battle-stage`/`.battle-stage__viewport` の再構成に伴うサブピクセル単位の
  layout 差が診断された）

- **判断根拠**: いずれの差分も意図しない退行ではなく、owner レビューが要求した P0-1/P0-2
  の修正（HUD の containing block を Canvas viewport に正しく一致させる）の意図した副作用
  である（§4 checklist の「意図した仕様変更か退行固定化かの判断」に基づく確認）。修正前は
  `[data-battle-ui-root]` の capture 領域が実際の Canvas よりも大きい矩形になっており、
  この不一致自体が owner の報告した「HUD が Canvas ではなく battle-stage 全体を基準に配置
  される」defect の根本原因だった。
- **evidence**: worktree `issue-1375-combat-hud-overlay`。ローカル環境（Linux, Playwright
  chromium, `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み）で
  `playwright test tests/e2e/phase-screens.spec.ts tests/e2e/visual-overlay.spec.ts
  tests/e2e/m2-combat-mvp.spec.ts --update-snapshots` により baseline を再生成した。
  #1374 / #1375 の前例（本ファイル上記セクション）で観測された CI runner とのフォント
  fallback 差に起因する追加差分が本 PR の CI 実行（run id `30696103540`, job id
  `91359166114`, e2e job, head sha `968af36ec82acc9fc6d20291888bbcc0313c0db3`）で
  8 件（`m2-running-hud-baseline` / `m2-timeout-overlay-baseline` / `title-menu` /
  `load-menu-empty` / `load-menu-available` / `load-menu-failure` /
  `preparation-default` / `preparation-upgrade-available`）再発したため、CI 実行結果の
  `test-results` artifact（artifact id `8817325110`）から該当する `*-actual.png` を
  取得し、それを最終 baseline として採用した（`running-hud-overlay-legacy-current` /
  `running-minimal-hud` は今回の CI 実行では許容差内に収まり差分なし）。各差分は目視確認の
  上、P0-1/P0-2 修正に伴う数px 単位の位置シフトであり、意図しない退行の固定化ではないことを
  確認した（§4 checklist 適用）。
- **maturity**: 変更なし（対象はすべて既存 `legacy-current` / `provisional` のまま）。
- **tolerance**: 変更なし（既存 `maxDiffPixels` 設定を維持）。
- **environment fingerprint**: 最終的に採用した baseline（上記 8 件）は CI 実行環境
  （GitHub Actions ubuntu runner, Chromium, Playwright config 既定 viewport,
  `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み, run id
  `30696103540`, job id `91359166114`）で生成された画像そのものである。

### legacy-result-surface コンパクト化に伴う baseline 更新（owner 実機再テスト対応、明示）

owner の実機プレイテスト（PR #1925 コメント issuecomment-5151416762、Windows/Chrome,
viewport 1437x1365）で「敗北してゲームが進行できなくなった」と報告された。原因は
`.legacy-result-surface`（3 panel 縦積み）の合計高さが `.battle-stage__viewport`
（Canvas 実描画高、`overflow: hidden`）を超え、`Return to hangar` ボタンが
`.battle-hud-layer` の `overflow-y: auto` スクロール範囲外（視認困難な位置）に
押し出されていたため。`src/style.css` の `.legacy-result-surface` 系セレクタ
（padding / gap / `.stat-grid` 関連）を圧縮し、3 panel が Canvas 実描画高内に収まる
よう修正した。この修正で以下 baseline との間に許容差を超える差分が生じた。

- `m2-timeout-overlay-baseline.png`（legacy-result-surface 全体を含む capture）
- `title-menu` / `load-menu-empty` / `load-menu-available` / `load-menu-failure` /
  `preparation-default` / `preparation-upgrade-available`（`phase-screens.spec.ts`、
  `[data-battle-ui-root]` capture。`.battle-hud-layer` 内のスペーシング変更が
  周辺レイアウトにも軽微に影響）

- **判断根拠**: 意図しない退行ではなく、owner 報告の defeat 進行不能 defect を修正する
  ための意図した spacing 圧縮の副作用である（§4 checklist 適用）。差分画像を目視確認し、
  `Return to hangar` ボタンが可視領域内に収まるようになったことを確認した（修正前は
  `test-results` artifact 上でボタンが panel 下端付近に押し出されていた）。
- **evidence**: worktree `issue-1375-combat-hud-overlay`。CI 実行（run id
  `30701007468`, job id `91371935097`, e2e job, head sha
  `b9cab3f679ce6777383afa0bf705ec323cc8f7bc`）で 7 件が再発したため、CI 実行結果の
  `test-results` artifact（artifact id `8818851481`）から該当する `*-actual.png` を
  取得し、それを最終 baseline として採用した。
- **maturity**: 変更なし。
- **tolerance**: 変更なし。
- **environment fingerprint**: 最終的に採用した baseline（上記 7 件）は CI 実行環境
  （GitHub Actions ubuntu runner, Chromium, run id `30701007468`, job id
  `91371935097`）で生成された画像そのものである。

### running-hud-paused / result-overlay-timeout の #1376 baseline 更新（明示）

Issue #1376（result screen / pause overlay を battle-stage 内 DOM overlay に統合する）は
AC12 の要求どおり、`data-phase-screen="pause"` / `data-phase-screen="result"` の単一
controller（`src/ui/phaseScreens.ts`）を実装した。この実装により、これまで
`pending-baseline`（`pending: no PNG/test`）で予約のみされていた `running-hud-paused` /
`result-overlay-timeout` の2行が、実際に撮影可能な `active-fixture-only` シナリオへ
昇格した。

- **old maturity**: `pending-baseline`（両行とも）
- **new maturity**: `legacy-current`（両行とも。`frozen` ではない — `frozen` 昇格には
  merged PR SHA / artifact digest / environment fingerprint が §4 の遷移規則どおり
  すべて揃う必要があり、#1377（旧 command rail 削除・E2E 検証）マージ後まで据え置く。
  これは `running-hud` / `running-hud-overlay-legacy-current` が採ってきた既存の
  `legacy-current` 運用と同じ扱いである）
- **transition reason**: `tests/e2e/visual-utils.ts` の `VISUAL_SCENARIO_STATUS`
  （`running-hud-paused` / `result-timeout` を `pending-fixture` -> `active-fixture-only`）
  および `VISUAL_BASELINE_REGISTRY_MATURITY`（`running-hud-paused` /
  `result-overlay-timeout` を `pending-baseline` -> `legacy-current`）を更新し、
  `tests/e2e/visual-overlay.spec.ts` に `[data-battle-ui-root]` を capture root とする
  active capture テストを2件追加した（`running-hud-overlay-legacy-current` と同一の
  capture root パターンをテンプレートとして踏襲）。
- **right-rail dependency**: pause dialog / result screen はいずれも
  `battle-screen-layer` 内の `role="dialog"` panel であり、`.command-rail` / right rail /
  two-column shell への依存はない（本 Issue の In Scope: 単一 controller が
  `battle-screen-layer` の DOM write を専有する設計）。
- **capture root**: `[data-battle-ui-root]`（`running-hud-overlay-legacy-current` と同一）。
- **determinism controls**: `installVisualScenario()` の `window.__LOOP_VISUAL_SCENARIO__`
  fixture（`paused: true` / `sortie.status: 'timeout'`）、`tests/e2e/visual.freeze.css`
  の font-family pin、`expectDomOverlayScreenshot()` の canvas mask、`maxDiffPixels: 100`
  （`running-hud-overlay-legacy-current` と同一の絶対ピクセル許容差）。
- **evidence status**: `active-pass`。ローカル worktree 環境（Linux, Playwright chromium,
  `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み）で
  `pnpm test:vrt:update:e2e` により2件の新規 baseline PNG
  （`vrt-running-hud-paused-overlay.png` / `vrt-result-timeout-overlay.png`）を生成し、
  直後に `--update-snapshots` なしで `playwright test tests/e2e/visual-overlay.spec.ts`
  を再実行して安定して PASS することを確認した（両 PNG とも既存の
  `vrt-running-hud-overlay.png` と同一の `[data-battle-ui-root]` capture root サイズ
  （1230x693）で撮影されている）。
- **known limitation（意図的に本 Issue の対象外とする観察事項）**: 現行のビューポート
  構成（PR #1925 で `.battle-stage__viewport` が Canvas 実描画矩形と `[data-battle-ui-root]`
  の capture 矩形を一致させた）では、`canvas` 要素の bounding box が capture root
  全体とほぼ同一になっており、`expectDomOverlayScreenshot()` の canvas mask
  （`mask: [page.locator('canvas')]`）が capture 矩形のほぼ全域を覆う。この結果、
  `running-hud-overlay-legacy-current` の既存 baseline、および今回追加した
  `running-hud-paused` / `result-overlay-timeout` の baseline は、いずれもほぼ全面が
  mask 色（solid magenta）で覆われた画像になっている（3枚とも計測環境でバイト単位で
  一致することを確認した）。これは pause dialog / result screen の実際の視覚差分に
  対する識別力が低い screenshot baseline であることを意味するが、この mask 挙動は
  `running-hud-overlay-legacy-current`（本 Issue より前から存在）にも同様に適用されて
  おり、本 Issue が新規に導入した defect ではない。capture root のジオメトリ設計を
  見直す場合は、別 Issue（#1377 または新規 follow-up）で `expectDomOverlayScreenshot()`
  の locator 選択や mask 戦略を再検討すること。
- **environment fingerprint**: ローカル環境（Linux, Playwright chromium 1223,
  viewport 1280x720、`playwright.config.ts` 既定の `deviceScaleFactor: 1`,
  `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み）で生成した。
  CI 実行環境（GitHub Actions ubuntu runner）で追加の font-rasterization 差分が
  発生した場合は、#1374/#1375 の前例（本ファイル上記セクション）と同様に CI
  `test-results` artifact の `*-actual.png` を最終 baseline として採用する運用を
  適用する。

### running-hud-overlay-legacy-current の canvas mask capture defect 修正（Issue #1980、明示）

owner レビュー（issuecomment-5172060362、REQUEST_CHANGES / P0 blocker）により、PR #1925
（コミット `968af36e`）が Canvas と `.battle-ui-layer` を同一 `.battle-stage__viewport` 内へ
移した結果、両者がほぼ同一の bounding box を持つようになり、`expectDomOverlayScreenshot()`
が適用していた Playwright `mask: [page.locator('canvas')]` が capture root 全面（HUD を含む）
を単色で覆ってしまう capture defect が判明した（旧 baseline は単色ピクセル比率 99.8% 以上）。

- **判断根拠**: HUD を実際に検証できない baseline は VRT として無意味であり、意図しない退行では
  なく capture 方式そのものの欠陥である。修正方針は、既存の `[data-visual-mask='true']` opt-in
  visibility 除外パターンを踏襲し、canvas を Playwright `mask` ではなく CSS `visibility: hidden`
  （`data-visual-canvas-hidden` 属性 + `tests/e2e/visual.freeze.css`）で除外する方式へ変更した
  （§4 checklist 適用。capture root を `[data-combat-hud]` に切り替える案は不採用 — Out of
  Scope、別 Issue 判断）。
- **capture policy 変更**: `tests/e2e/visual-utils.ts` の `expectDomOverlayScreenshot()` に
  `canvasVisibility` オプション（既定 `'mask'` = 従来挙動、`'hidden'` = 新方式）を追加。
  `running-hud-overlay-legacy-current` の呼び出し箇所（`tests/e2e/visual-overlay.spec.ts`）のみ
  `canvasVisibility: 'hidden'` を指定し、他の DOM overlay baseline は既定値のまま変更していない。
- **機械的な検証追加**: 単一色ピクセル比率が閾値（0.9）未満であることを canvas `getImageData`
  で検証する pixel diversity test、および `[data-combat-hud]` を意図的に非表示化すると screenshot
  assertion が確実に fail することを確認する negative control test を
  `tests/e2e/visual-overlay.spec.ts` に追加した。
- **evidence**: worktree `.claude/worktrees/issue-1980-vrt-canvas-hidden-hud`。ローカル環境
  （Playwright chromium v1223、`pnpm run test:vrt:update:e2e` で `VITE_E2E_MODE=true pnpm build`
  済みの dist を `LOOP_VRT_LANE=true` で配信、`playwright.config.ts` 既定 viewport 1280x720、
  `tests/e2e/visual.freeze.css` の generic `sans-serif` 固定込み）で候補 PNG を生成し、
  expected/actual を目視確認した（HUD の Hull/Kills/Elapsed/Weapon/Assist/Pause が実際に描画さ
  れていることを確認）。`pnpm run test:vrt:e2e` で pixel diversity / negative control を含む
  全 4 件のアクティブテストが PASS することを確認した。CI 実行環境での再現確認は本 PR のレビュー
  プロセスで行う（candidate producer はこの worktree、canonicalization authority は本 PR の
  レビュー・マージ）。
- **maturity**: 変更なし（`legacy-current` のまま。`frozen` 化条件は変更なし）。
- **tolerance**: 変更なし（`maxDiffPixels: 100`）。capture 内容が canvas 単色塗り潰しから HUD
  実描画へ変わったため、絶対ピクセル数の意味が「単色領域を除いた非mask領域の差分」から「HUD 全体
  領域の差分」へ変わった点に注意。

### running-hud-overlay-legacy-current の negative control 実装欠陥修正（Issue #1980 iteration 1、明示）

独立 test-runner 検証（PR #1988 レビュー、iteration 1 fix_delta）により、上記セクションで追加した
negative control test（`[data-combat-hud]` を意図的に非表示化すると screenshot assertion が
fail することを確認するテスト）が実際には fail せず、`.rejects.toThrow()` が resolved
（意図せず PASS）してしまう欠陥が判明した。

- **根本原因**: negative control test が、実 baseline capture（AC5/AC6 テスト）と同じ
  snapshot 名（`'vrt-running-hud-overlay.png'`）を `expectDomOverlayScreenshot()` /
  `toHaveScreenshot()` 経由で指定していたため、AC2 の VC コマンドである
  `pnpm run test:vrt:update:e2e`（`--update-snapshots=all`）実行時、`toHaveScreenshot()` は
  比較せず常に対象ファイルへ上書き保存する。ファイル内のテスト実行順（AC5/AC6 → pixel diversity
  → negative control）により、negative control の「HUD 非表示」capture が最後に同名ファイルへ
  上書きされ、実 baseline PNG 自体を HUD が写らない空の capture へ静かに破壊していた
  （このセクション冒頭で「HUD の Hull/Kills/Elapsed/Weapon/Assist/Pause が実際に描画されている
  ことを確認した」と記録した候補 PNG は、この経路で committed baseline から後日破壊されたことを
  worktree 上の未コミット diff で確認済み）。この結果、`toHaveScreenshot` の比較対象 baseline
  自体が既に HUD 非表示状態と近似していたため、negative control が「差分なし」と判定して
  resolved していた。
- **判断根拠**: `--update-snapshots=all` を使う AC2 の VC コマンドと同じ snapshot 名を
  negative control が共有する設計は、negative control 自身が実 baseline を破壊しうる
  構造的欠陥であり、修正対象は `In Scope` の「negative control test の追加」の実装詳細
  （snapshot 名選択）であって、Issue #1980 の capture policy（`canvasVisibility: 'hidden'`）
  自体の妥当性ではない。
- **修正内容**: negative control test を `toHaveScreenshot()` の snapshot 読み書きパイプライン
  から独立させ、`Locator.screenshot()` で直接取得した「HUD 非表示」capture と、ディスク上の
  committed baseline PNG（読み取り専用）を、pixel diversity test と同じ `canvas` 2D
  `getImageData()` 手法でブラウザ内 pixel-diff し、差分ピクセル数が実アサーションの
  `maxDiffPixels: 100` を上回ることを確認する方式へ変更した（`tests/e2e/visual-overlay.spec.ts`）。
  この方式は `--update-snapshots` フラグの有無に関わらず baseline ファイルへ一切書き込まない。
- **evidence**: worktree `.claude/worktrees/issue-1980-vrt-canvas-hidden-hud`。修正後、
  `pnpm run test:vrt:update:e2e` を複数回実行しても baseline PNG が安定（2回目以降
  re-generate 差分なし）で HUD 実描画のまま保たれることを確認し、`pnpm run test:vrt:e2e`
  （4 active test 全 PASS、3 pending-baseline skip）、および AC2 / AC3 個別実行（いずれも
  PASS）で再検証した。
- **maturity / tolerance**: 変更なし。

### `phase-screens.spec.ts` VRT baseline 6 件の更新見送り判断（Issue #1986、明示）

Issue #1986 は、PR #1984（Issue #1376）由来の E2E 不整合の follow-up として、
`phase-screens.spec.ts` の VRT baseline 6 件（`title-menu` / `load-menu-empty` /
`load-menu-available` / `load-menu-failure` / `preparation-default` /
`preparation-upgrade-available`）が CI `e2e` job で mismatch しているという観測を
出発点としていた。実装着手時（`scope_revalidation.base_sha`
`1c4f938bab337723b65d8fe87d75e64180d3e399`、PR #1998 マージ後の `origin/main`）に
再調査した結果、**baseline 更新は不要と判断し、6 件とも変更していない**。

- **判断根拠**: `base_sha` と同一コミット（`1c4f938b`）に対する直近 CI 実行
  （run id `31049091799`, job `e2e`, `2026-08-05T21:32:40Z`, `conclusion: success`）を
  `gh run view --log` で確認したところ、`title-menu` / `load-menu-empty` /
  `load-menu-available` / `load-menu-failure` / `preparation-default` /
  `preparation-upgrade-available` の 6 件すべてが現行コミット済み baseline に対して
  **PASS** していた（`running-minimal-hud` および `m2-combat-mvp.spec.ts` の
  `timeout overlay baseline` / `running HUD baseline` を含む他の screenshot test も
  同一 CI 実行で全件 PASS）。CI が正本環境（`mcr.microsoft.com/playwright@sha256:
  9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948`、
  Playwright `1.60.0`、`chromium`）で既に一致しているため、Issue 起票時点の
  「CI で mismatch している」という観測は、その後 PR #1998 のマージにより解消済みの
  stale な情報だったと判断した。
- **ローカル再現との切り分け**: 同一環境（Playwright `1.60.0`、`chromium`、
  `VITE_E2E_MODE=true pnpm build` 後の `dist` を `CI=true pnpm exec playwright test
  tests/e2e/phase-screens.spec.ts --project=chromium` で実行、Linux ローカルホスト）で
  実行したところ、対象 6 件すべてで `ratio 0.01` 前後の小さな pixel diff が発生し FAIL した
  （`running-minimal-hud` を含む他の phase-screens テストおよび `m2-combat-mvp.spec.ts` の
  `timeout overlay baseline` は PASS、`running HUD baseline` のみ同種の `ratio 0.02` diff で
  FAIL）。同一パターン（複数 screenshot test に一様に生じる小さい pixel diff、CI では
  PASS）は、本台帳 §1/§5 で既知の「CI runner とのフォント fallback 差」に一致し、
  意図した DOM/CSS 変更ではなく local-vs-CI 環境差（フォントレンダリング）由来と判断した。
  Issue #1986 の Stop Condition（「差分がフォント・DPR・ブラウザバージョン等の環境差と
  区別できない場合」「clean current-main でも同一の VRT 差分が再現する場合」）に該当するため、
  baseline は更新しない。
- **AC2/AC3 修正との切り分け**: 本 Issue で `tests/e2e/m2-combat-mvp.spec.ts:780` の
  `data-legacy-result-surface` → `data-legacy-debrief-surface` 修正を行った際、この修正が
  `m2-timeout-overlay-baseline.png` の canvas capture に視覚的影響を与えるかを
  `src/systems/PhaseTransitionSystem.ts` / `src/ui/HudController.ts` のソース読解で
  検証した。`running` → `sortie_terminal` の遷移先は `result`（`debrief_pending_reward` /
  `debrief_reward_claimed` ではない）であり、`isLegacyDebriefPhase()` は
  `debrief_pending_reward` / `debrief_reward_claimed` のみを true とするため、timeout
  シナリオ実行中 `data-legacy-debrief-surface` 要素は常に `hidden` のままである。よって
  masking selector の属性名修正は当該テストに対して視覚的に no-op であり、上記ローカル
  FAIL はこの修正由来ではなく環境差由来と結論した。
- **evidence**: worktree `.claude/worktrees/issue-1986-e2e-followup-1984`。
  `git status --porcelain -- tests/e2e/__screenshots__/phase-screens.spec.ts/` は
  空（変更なし）。CI run `31049091799`（job `e2e`）のログに全 6 件の PASS 行を確認。
- **maturity / tolerance**: 変更なし（baseline PNG 6 件は既存のまま）。

## 4. baseline update policy（更新ポリシー）

### 自動更新の禁止

- **CI が PNG を生成して正とする運用（snapshot auto-update）を禁止する。** baseline PNG の
  追加 / 更新は必ず人間の commit / review を前提とする。
- CI ジョブで `--update-snapshots` 系のフラグを常用しない。意図しない退行をそのまま「正」に
  固定するリスクを避けるため。

### baseline 候補の生成者・正本採用権限・永続化の境界

- **candidate producer** は差分候補 PNG を生成する環境である。pinned Playwright container を
  含む CI artifact、または互換性を確認したローカル環境は candidate を生成してよい。
- **canonicalization authority** は candidate を repository の baseline として採用する人間の
  判断である。candidate が生成された時点では canonical baseline ではない。
- **persistence** は、baseline PNG を含む reviewed PR が commit・merge されることを指す。
  candidate を artifact として保存するだけでは persistence でも canonicalization でもない。
- required CI は `playwright.config.ts` の `updateSnapshots: 'none'` 解決と
  `scripts/check-vrt-snapshot-policy.py` により、既知の Playwright/Vitest update mode と
  `test:vrt:update:e2e` への到達経路を拒否する。validator は `ci.yml`、そこから参照される
  local composite action、到達した package script だけを構造的に解析し、未解決 interpolation・
  malformed YAML・validator wiring 欠落を fail-closed とする。
- `pnpm test:vrt:update:e2e` は local/manual candidate generation 専用の明示入口であり、
  required CI から直接・間接に実行してはならない。component VRT の update script は #1389 の
  責務であり、本 policy に含めない。
- この静的 policy は repository rules、CODEOWNERS、required review、または人間による review
  実施を強制しない。それらの enforcement は repository 設定と運用契約の責務である。

### baseline 変更 PR のレビュー checklist（基準画像変更のレビュー項目）

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
  `outputs.artifact-url` / `outputs.artifact-id`、および必須 fingerprint トークン（runner /
  node / Playwright / browser / project / viewport / deviceScaleFactor /
  snapshotPathTemplate）を含む。
- **capture 単位の cross-validation（嘘防止、PR #1813 review fix で単一 suite フィンガープリント
  から全面移行）**: suite 単位の固定フィンガープリントは存在しない。summary が declare する
  `CAPTURES` 配列の **全フィールド**（`spec_file` / `screenshot_name` / `registry_id` /
  `directory` / `browser` / `project` / `viewport` / `device_scale_factor` / `comparator_kind` /
  `comparator_value` / `style_path` / `artifact_scope` / `digest_env` / `retention_days`）を、
  `tests/e2e/*.spec.ts`・`playwright.config.ts`・`tests/e2e/visual-utils.ts`・本台帳（registry
  id ⇔ screenshot filename の対応）から独立に再導出した値と 1 フィールドずつ完全一致検査する。
  いずれかのフィールドが drift した場合は hard fail する。

### artifact upload（証跡アップロード）

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

- VRT evidence upload step は、現行 validator の許可 pin に合わせて **`@v6` に固定**する。
  リポジトリ全体の `upload-artifact` major を統一する主張ではなく、VRT 証跡配線だけの運用方針である。
  `outputs.artifact-url`（summary が依存する出力）は v4 以降で提供される公式出力であり `@v6` で利用可能。
- major bump（例 `@v7`）は VRT 証跡基盤の方針変更として、本台帳のバージョン方針と検証
  スクリプトの許可 pin（`ALLOWED_UPLOAD_USES`）を同時に更新する別レビューで行う。検証スクリプト
  が許可 major を明示的に保持することで、暗黙のバージョン drift を防ぐ。

### summary step（環境 fingerprint と artifact link の要約）

- summary step は **artifact upload step の後** に置く。`outputs.artifact-url` は upload 後にしか
  得られないため（`$GITHUB_STEP_SUMMARY` は step ごとのファイルで、後続 step から過去 summary を
  書き換えられない）。
- summary step は `$GITHUB_STEP_SUMMARY` に以下の環境 fingerprint と artifact link を記録する:
  runner / OS（`RUNNER_OS` / `RUNNER_ARCH` / image version）・Node version・Playwright version・
  browser（chromium）・project（`chromium` / `Desktop Chrome`）・viewport（1280x720）・
  deviceScaleFactor（1）・`snapshotPathTemplate`・`outputs.artifact-url`・
  `outputs.artifact-digest`（GitHub 公式 digest。capture 単位の digest 列が参照する）。
  baseline path や screenshot options（`animations` / `maxDiffPixels`）といった suite 単位の
  固定値は記録しない — capture ごとに directory / comparator が異なり得るため、下記
  「capture 単位の CI 証跡契約」の表でのみ表現する。
- actual / expected / diff は **visual mismatch が発生した場合のみ** `test-results` artifact 内
  の参照として記録し、存在しない場合（pass 時）は **N/A と明記する**。常に link を要求しない
  （pass run では actual/expected/diff が存在しないため）。

### capture 単位の CI 証跡契約（Issue #1387 で追加）

- CI summary と `scripts/check-visual-artifact-pipeline.py` は、e2e ジョブ全体を単一の
  suite フィンガープリントとして扱うのではなく、個々の VRT **capture**（test file 内の
  個別の `toHaveScreenshot()` / `expectDomOverlayScreenshot()` 呼び出し）を列挙する。
- 各 capture の screenshot directory / browser / project / viewport / deviceScaleFactor /
  comparator 種別と値（`maxDiffPixels` または `maxDiffPixelRatio` のいずれか一方）/
  stylePath 有無 / artifact scope（`job` か `suite` か）/ artifact URL / digest /
  retention を summary が出力し、validator がこれを spec ファイル・
  `playwright.config.ts`・`tests/e2e/visual-utils.ts` の registry maturity・本台帳の
  `artifact/test` 列（screenshot filename ⇔ registry id の対応。`toHaveScreenshot()` の直接
  呼び出しは呼び出し箇所自体に registryId を持たないため）から再導出した値と cross-validate
  する（drift すれば hard fail）。
- digest は GitHub `upload-artifact` の公式 `outputs.artifact-digest` を使う。リポジトリ側で
  独自計算する tree digest は使わない — hidden file の扱いなど upload 対象集合と食い違い得る
  ローカル計算より、プラットフォーム自身の digest を正本とする。
- 現行の active capture はすべて標準 `e2e` ジョブ内で実行され、`test-results/` /
  `playwright-report/` という単一のジョブ単位 artifact を共有する
  （artifact scope: `job`。suite 単位の分離 artifact は現状存在しない）。
- `pending-baseline`（maturity）の registryId を参照する capture は、
  `expectDomOverlayScreenshot()` 側で実行時に fail closed するため、決して
  active capture として宣言してはならない。validator はこれを registry
  maturity 側から独立に検査する。
- validator が静的に解釈できない `toHaveScreenshot()` 呼び出し（options-only の暗黙 name、
  引数なし呼び出し、name/registryId が解釈不能な呼び出し）は、黙って active capture 列挙から
  除外（skip）せず hard fail する。実在する capture が気づかれずに証跡契約から漏れることを防ぐ。

### component VRT / active suite 依存（コンポーネント視覚テストの前提）

- Issue #1389 で `@vitest/browser-playwright@4.1.6` と `vitest.visual.config.ts` を
  導入し、`tests/component/combat-hud-running.vrt.test.ts`（`combat-hud-running` 1
  シナリオのみ、maturity `provisional`）を追加した。ただし
  `running-hud-paused` / `result-overlay-timeout` / `final-no-command-rail`
  （result / pause modal / final-no-command-rail 等の他シナリオ）は本 Issue の
  Out of Scope のまま `pending-baseline` を維持する（対象 UI の stable module 化が
  進んだ後続 Issue で扱う）。
- **Playwright / Vitest baseline root 分離契約**: Playwright E2E VRT の baseline は
  `tests/e2e/__screenshots__/` 配下にのみ置く。Vitest component VRT の baseline は
  専用の別ルート `tests/component/__screenshots__/` 配下にのみ置き、両者の
  ディレクトリを混線させない。
  `scripts/check-visual-artifact-pipeline.py` はこの 2 ルートを定数として保持し、
  capture の宣言ディレクトリがどちらのルートにも一致しない、または誤って
  Vitest 側ルートに置かれている場合は hard fail する。
- **component VRT の comparator 監査（AC10, Issue #1389）**:
  `scripts/check-visual-artifact-pipeline.py` は
  `tests/component/**/*.vrt.test.ts` の `.toMatchScreenshot()` 呼び出しを
  `extract_derived_vitest_component_captures()` で静的に再導出し、
  `validate_vitest_component_captures()` で comparator（`allowedMismatchedPixels` /
  `allowedMismatchedPixelRatio` / `threshold` のいずれか）と snapshot root が
  `tests/component/__screenshots__/` 配下であることを hard fail 検査する。この
  audit は `jobs.e2e` の宣言済み `CAPTURES` 配列との cross-validation（Playwright
  専用、上記「capture 単位の CI 証跡契約」参照）とは独立しており、
  `component-vrt-report` CI job は non-required/report-only のため対応する
  declared-in-workflow の `CAPTURES` 配列は持たない。
- **CI 配線（Issue #1389）**: `.github/workflows/ci.yml` の `component-vrt-report`
  job は non-required（branch protection の required check には登録しない）で
  `pnpm test:vrt:component` を実行し、`continue-on-error` / `|| true` を使わず
  実際の exit code をそのまま job の conclusion に反映する。artifact upload と
  summary step は `if: ${{ always() }}` で実行し、
  `tests/component/__screenshots__/`（committed baseline と、mismatch 時の
  actual/expected/diff）を artifact として保存する。この job の pass/fail が
  `ci-verdict-summary` の merge-ready 判定を絶対にブロックしないよう、
  `.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py` の
  `CLASSIFICATION_MAP` に `("ci", "component-vrt-report"): "excluded"` を登録
  済み（Scope Delta）。
- `active CI suites` と `check-visual-artifact-pipeline` の `cross-validation` が揃っていない場合、`legacy-current` / `pending-baseline`
  からの frozen 昇格は保留する。frozen 昇格時には `merged PR SHA`、`CI summary`、`artifact path` を再確認する。
- `focus` / `inert` / `keyboard` / `dialog` の振る舞いを理由に frozen 昇格を代替しない。#1373-#1376 の
  `behavior test`（フォーカス移動 / inert 化 / キーボード遷移 / dialog 開閉）が完了していることを
  先行条件として参照する。

### component VRT の PR #1977 review fix（OWNER REQUEST_CHANGES 対応、明示）

PR #1977（Issue #1389）は OWNER の詳細な REQUEST_CHANGES（既存 `impl-review-loop: APPROVE`
を上書きするマージ前必須条件）を受け、以下 5 点を修正した。

- **hidden attachment upload の欠落**: `.github/workflows/ci.yml` の
  `component-vrt-report` job の `actions/upload-artifact@v6` step に
  `include-hidden-files: true` を追加した。`.vitest-attachments/` は隠しディレクトリ
  （dot-prefixed）であり、`include-hidden-files` なしでは失敗時の actual/diff 画像が
  silent に artifact から欠落し得る問題を修正した。加えて、意図的な visual mismatch を
  発生させる controlled negative control fixture
  （`tests/component/__negative_control__/hidden-attachment-negative-control.vrt.test.ts`。
  実 baseline とは別の committed reference PNG を持つ throwaway 用途。
  `VITE_VRT_NEGATIVE_CONTROL=true` gate で通常実行時は skip）を追加し、CI 側で
  upload → download → `*-actual-*.png`/`*-diff-*.png` の存在確認まで実施することで、
  「YAML が正しく見える」だけでなく hidden-file upload が実際に機能することを証明する。
- **snapshot root 監査の循環参照**: `scripts/check-visual-artifact-pipeline.py` の
  `extract_derived_vitest_component_captures()` が自己生成した期待値と自己比較していた
  circularity を修正した。`vitest.visual.config.ts` の実際の `test.include` パターンと
  screenshot directory 設定（`parse_vitest_visual_config()`）から directory を導出し、
  実際に committed された PNG ファイルと突合する。`vitest.visual.config.ts` は
  Vitest 4.1.6 の `browser.screenshotDirectory` shorthand が config 正規化時に絶対パス化
  され `@vitest/browser` 側の `resolveScreenshotPath` デフォルトと二重結合してバグる
  （ローカル再現済み）ため、`browser.expect.toMatchScreenshot.resolveScreenshotPath` を
  明示実装する形で AC4 を満たす（`vitest.visual.config.ts` 内コメント参照）。`test.include`
  も `tests/component/*.vrt.test.ts`（非再帰）に制限し、nested spec が audit 対象外の
  root へ silent に解決される経路を防いだ。mutation test
  （`test_extract_derived_vitest_component_captures_mutation_wrong_screenshot_directory_fails`）
  で、config の screenshot directory を変更すると validator が実際に fail することを確認した。
- **comparator 監査の Vitest 仕様不一致**: `allowedMismatchedPixels` /
  `allowedMismatchedPixelRatio` を mutually exclusive として拒否していた誤りを修正し、
  Vitest 同様に両方の併記を許可した（厳しい方が適用される）。コメント/文字列を mask した
  構造解析（`_parse_vitest_comparator_options()`）に置き換え、コメントアウトされた
  comparator option が誤検出されないことを regression test で確認した。数値範囲
  （ratio/threshold は `[0, 1]`、pixels は非負整数）・型検証、`comparatorOptions` の
  直接プロパティのみを対象とする scoping、spread/変数参照/計算プロパティの fail-closed
  も追加した。
- **capture 0 件時の silent pass**: `cross_validate_component_vrt_captures()` を追加し、
  `docs/dev/visual-baseline-registry.md`（本ファイル）の component VRT registry
  エントリと、derived capture を 1 対 1 で cross-validate する。capture 0 件、
  missing PNG、orphan PNG、重複 capture id、comparator/directory drift を
  hard fail にする。
- **freeze CSS 未再利用**: `tests/component/combat-hud-running.vrt.test.ts` が
  `tests/e2e/visual.freeze.css`（read-only import。当該ファイル自体は Allowed Paths 外の
  ため変更していない）を読み込むよう変更した。mount container には既に
  `data-battle-ui-root` 属性があり、freeze CSS の font-family pin / animation 停止が
  直接適用される。baseline PNG は CI-pinned Docker container
  （`mcr.microsoft.com/playwright@sha256:9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948`。
  前回 iteration と同一 digest）内で freeze CSS 適用後に再生成し、同一 container 内で
  複数回実行して pixel drift がないことを確認した（evidence は PR #1977 本文 / commit
  message に記録する）。

## 6. 関連

- #681（`toHaveScreenshot` baseline 追加 / auto-update pipeline は out of scope）
- #727（Canvas HUD 集約・layout 再設計 / deferred。本台帳の `provisional` / `predicate-only`
  契約を無効化し得る）
- #747（`m2-timeout-overlay-baseline.png` の CI レンダリング再生成 hotfix）
- #726（HUD 整数段階表示）/ #732（timeout 中立 terminal）/ #222（PR テンプレート Runtime
  Verification Evidence）
