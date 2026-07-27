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
| running-hud | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/m2-combat-mvp.spec.ts/m2-running-hud-baseline.png`（`m2-combat-mvp.spec.ts` の running HUD baseline test、`[data-field="sortie-status"]` 単一 field、118x66px） | #681 / #726 / #727 / #1370 / #1375 / #1377 / #1380 | running HUD が描画されること・HULL/HP の小数露出がないこと・桁溢れがないこと | 色味 / 詳細配置 / right rail 依存は再設計まで可変 | `maxDiffPixelRatio: 0.08`（PR #1721 review fix で実コードに合わせて修正。理由: legacy-current・単一 field の小capture。#727 再設計時に再評価する） | #727 再開時または #1370 / #1375 / #1377 / #1380 系の overlay rollout 進行時に破棄 / 再分類可 | #727 / #1370 / #1375 / #1377 / #1380（HUD/layout 再設計と overlay rollout） |
| running-hud-overlay-legacy-current | screenshot-baseline | legacy-current | `tests/e2e/__screenshots__/visual-overlay.spec.ts/vrt-running-hud-overlay.png`（`tests/e2e/visual-overlay.spec.ts` の `[data-battle-ui-root]` DOM overlay baseline test） | #1386 / #1380 / #1370 / #1374 | `[data-battle-ui-root]` DOM overlay 全体（HUD 各 field を含む）が描画されること。`running-hud`（`m2-combat-mvp.spec.ts` の単一 field baseline）とは別の独立した baseline であり、両者は衝突しない | 色味 / 詳細配置 / right rail・command-rail 依存は再設計まで可変。将来の overlay 再設計で `frozen` 化するまでは legacy-current のまま | `maxDiffPixels: 100`（絶対ピクセル数。理由: capture root が canvas mask を含み全体の過半を占めるため `maxDiffPixelRatio` は不採用。非mask領域のfont-rasterizationノイズに対しこのworktree環境で複数回実測し PASS した最小幅に margin を加えた値。`tests/e2e/visual.freeze.css` で capture root 配下の font-family を generic family（`sans-serif`）に固定し、host font fallback chain 依存を除去済み） | §4 の `maturity transition` を満たした時点で `legacy-current -> frozen`（#1375/#1376/#1377 マージ後） | #1375 / #1376 / #1377 / #1380（overlay UI 実装マージで破棄・再生成対象） |
| defeat-overlay | pixel-contract | predicate-only | `getImageData` smoke（`m2-combat-mvp.spec.ts` の defeat overlay 赤支配ピクセル検証 / AC8） | #681 / #732 | defeat overlay が赤系・終端状態として識別可能であること | exact pixels は未固定。最終 layout / 色味は未確定 | N/A（screenshot baseline ではない） | predicate（赤支配）が壊れた場合のみテスト側を調整 | #727 |
| hp-label | predicate-only | predicate-only | HP label bounds smoke（`m2-combat-mvp.spec.ts` の HP label bounding box 検証 / AC5） | #726 / #727 | HP label が viewport 外 / NaN 表示にならない・bounds 内・可読であること | 最終 UI 表現 / 配置は未固定 | N/A（screenshot baseline ではない） | predicate（bounds / 可読）が壊れた場合のみテスト側を調整 | #727 |
| running-hud-paused | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1375 / #1376 / #1377 / #1391 | running HUD の停止状態でも command-rail / right rail / two-column shell / `.battle-stage` 外 controls への依存がないことを明示し、pause overlay の focus / inert / keyboard 証跡を #1376 側で確認する | frozen 適用対象外。duration 等の固定は `durationMs` / `fixedDeltaMs` で判定可能な場合に限定 | pending: no PNG/test（active PASS claim 保留） | §4 の `maturity transition` を満たした時点で `pending-baseline -> frozen` | #1370 / #1375 / #1376 / #1377 / #1380 |
| result-overlay-timeout | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1376 / #1377 / #1392 | result overlay timeout の timeout 時間表現を、`elapsedTicks` 由来表示ではなく `durationMs` / `fixedDeltaMs` を優先し、result overlay の focus / inert / keyboard / dialog 証跡を #1376 側で確認する | 右寄り controls への依存や focus / inert / keyboard / dialog 条件は frozen 直前に検証 | pending: no PNG/test（active PASS claim 保留） | §4 の `maturity transition` を満たした時点で `pending-baseline -> frozen` | #1380 / #1376 / #1377 |
| final-no-command-rail | screenshot-baseline | pending-baseline | pending: no PNG/test | #1380 / #1370 / #1377 | 最終結果画面が `command rail` 未依存でも意図読取できること。right rail / battle-stage 外依存は frozen 禁止条件 | #1370 / #1377 の影響条件を満たすまで固定化しない | pending: no PNG/test（active PASS claim 保留） | `merged PR SHA` と `artifact URL` / `artifact digest` / `environment fingerprint` が確定した時点で `legacy-current -> frozen` | #1380 / #1370 / #1377 |

### pending-baseline / legacy-current の遷移規則

`legacy-current` / `pending-baseline` から `frozen` へ変更する際は、以下を満たすこと。

- merged PR SHA、active test path、committed PNG path、right rail / command-rail / two-column 依存除外の
  レビュー記録がそろっていること。
- `artifact URL` は CI summary から復元可能であること。`artifact digest` は現行 CI summary /
  `check-visual-artifact-pipeline.py` では未配線のため、#1387 で `artifact-digest` の記録と検証が
  実装されるまで frozen 昇格不可とする。
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
予約行の確認として、`running-hud-paused` / `result-overlay-timeout` / `final-no-command-rail` は `pending-baseline` で `pending: no PNG/test` と明記する。
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
required_pending = {
    "running-hud-paused",
    "result-overlay-timeout",
    "final-no-command-rail",
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
- **evidence**: PR #1815（`worktree-issue-1374-phase-screen-overlay`）。`test-results/` の
  `*-actual.png` / `*-diff.png` で差分を目視確認した上で baseline を再生成した
  （`playwright test tests/e2e/visual-overlay.spec.ts --update-snapshots`）。再生成後は
  同一コマンドの非 update 実行で PASS することを確認済み。
- **maturity**: 変更なし（`legacy-current` のまま）。`frozen` 化条件（#1375/#1376/#1377
  マージ後）は本更新の対象外。
- **environment fingerprint**: 本更新はローカル worktree 環境（Linux, Chromium,
  Playwright config 既定 viewport, `tests/e2e/visual.freeze.css` の generic
  `sans-serif` 固定込み）で生成した。CI 環境（GitHub Actions ubuntu runner）との
  fingerprint 一致は、本 PR の CI `e2e` job の実行結果で確認する
  （§5 の summary step 参照）。差異が生じた場合は追加の baseline 再生成が必要になる。

## 4. baseline update policy（更新ポリシー）

### 自動更新の禁止

- **CI が PNG を生成して正とする運用（snapshot auto-update）を禁止する。** baseline PNG の
  追加 / 更新は必ず人間の commit / review を前提とする。
- CI ジョブで `--update-snapshots` 系のフラグを常用しない。意図しない退行をそのまま「正」に
  固定するリスクを避けるため。

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
  `outputs.artifact-url`、および必須 fingerprint トークン（runner / node / Playwright / browser /
  project / viewport / deviceScaleFactor / snapshotPathTemplate / baseline path / animations）を含む。
- **fingerprint cross-validation（嘘防止）**: summary が echo する `viewport` /
  `snapshotPathTemplate` / `maxDiffPixels` を `playwright.config.ts` と
  `tests/e2e/m2-combat-mvp.spec.ts` の実値と照合する。config / spec を変更して summary を
  更新し忘れた（またはその逆）場合は fail する。これにより fingerprint が「人間向けメモ」では
  なく実際の比較条件を表す監査情報であることを保証する。

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
  deviceScaleFactor（1）・`snapshotPathTemplate`・baseline path・screenshot options
  （`animations` / `maxDiffPixels`）・`outputs.artifact-url`。
- actual / expected / diff は **visual mismatch が発生した場合のみ** `test-results` artifact 内
  の参照として記録し、存在しない場合（pass 時）は **N/A と明記する**。常に link を要求しない
  （pass run では actual/expected/diff が存在しないため）。

### component VRT / active suite 依存（コンポーネント視覚テストの前提）

- 現行 repo には `@vitest/browser-playwright` と `vitest.visual.config.ts` が未導入である。
  これらと対象 overlay module の 3 要件が揃うまでは、Component VRT は未導入として扱い、
  上記 `running-hud-paused` / `result-overlay-timeout` /
  `final-no-command-rail` は `pending-baseline` 維持とする。
- `active CI suites` と `check-visual-artifact-pipeline` の `cross-validation` が揃っていない場合、`legacy-current` / `pending-baseline`
  からの frozen 昇格は保留する。frozen 昇格時には `merged PR SHA`、`CI summary`、`artifact path` を再確認する。
- `focus` / `inert` / `keyboard` / `dialog` の振る舞いを理由に frozen 昇格を代替しない。#1373-#1376 の
  `behavior test`（フォーカス移動 / inert 化 / キーボード遷移 / dialog 開閉）が完了していることを
  先行条件として参照する。

## 6. 関連

- #681（`toHaveScreenshot` baseline 追加 / auto-update pipeline は out of scope）
- #727（Canvas HUD 集約・layout 再設計 / deferred。本台帳の `provisional` / `predicate-only`
  契約を無効化し得る）
- #747（`m2-timeout-overlay-baseline.png` の CI レンダリング再生成 hotfix）
- #726（HUD 整数段階表示）/ #732（timeout 中立 terminal）/ #222（PR テンプレート Runtime
  Verification Evidence）
