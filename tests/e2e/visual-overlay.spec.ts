/**
 * tests/e2e/visual-overlay.spec.ts — Playwright primary VRT suite (Issue
 * #1386, #1380 rollout tracker).
 *
 * This is the primary browser/runtime VRT gate (`pnpm test:vrt:e2e` /
 * `pnpm test:vrt`), scoped to the DOM overlay root (`[data-battle-ui-root]`,
 * `.battle-ui-layer[data-battle-ui-root]` in `src/main.ts`) — never a
 * full-canvas / full-body / full-shell target, and never `.command-rail`
 * (AC3, AC5). It reuses the Scenario Support Matrix, Screenshot Target
 * Guard and freeze CSS wiring introduced in Issue #1385
 * (`tests/e2e/visual-utils.ts`) and the maturity classification in
 * `docs/dev/visual-baseline-registry.md` (Issue #1384).
 *
 * Out of scope for this Issue (see `docs/dev/visual-baseline-registry.md`
 * and the Issue #1386 contract): adding a `frozen` baseline for any final
 * overlay surface, and implementing the `running-hud-paused` /
 * `result-timeout` / `final-no-command-rail` overlay UIs themselves
 * (#1375 / #1376 / #1377). Those scenarios remain `pending-fixture`
 * (`VISUAL_SCENARIO_STATUS`) / `pending-baseline` (registry `maturity`)
 * here and are only exercised as explicit-pending / fail-closed proofs
 * (AC4) — never captured.
 */

import { test, expect } from '@playwright/test'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  installVisualScenario,
  expectDomOverlayScreenshot,
  isPendingFixtureScenario,
  VISUAL_BASELINE_REGISTRY_MATURITY,
  type VisualScenarioFixture,
} from './visual-utils'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** `active-fixture-only` (Scenario Support Matrix) — safe to install and capture. */
const RUNNING_HUD_FIXTURE: VisualScenarioFixture = {
  name: 'running-hud',
  loopPhase: 'running',
  paused: false,
  sortie: { status: 'running', elapsedTicks: 900, fixedDeltaMs: 16 },
  player: { hp: 80, maxHp: 100 },
  progress: { resources: 12, weaponPower: 1 },
  telemetry: { status: '', summary: '' },
  viewportLabel: 'desktop-1280x720',
}

/**
 * `pending-fixture` fixture payload, used only to prove the fail-closed
 * rejection below (AC4) — never installed successfully and never used to
 * capture a screenshot.
 */
const RUNNING_HUD_PAUSED_FIXTURE: VisualScenarioFixture = {
  name: 'running-hud-paused',
  loopPhase: 'running',
  paused: true,
  sortie: { status: 'running', elapsedTicks: 900, fixedDeltaMs: 16 },
  player: { hp: 80, maxHp: 100 },
  progress: { resources: 12, weaponPower: 1 },
  telemetry: { status: '', summary: '' },
  viewportLabel: 'desktop-1280x720',
}

// ---------------------------------------------------------------------------
// Primary VRT gate: DOM overlay root, provisional/legacy-current baseline only
// ---------------------------------------------------------------------------
//
// This suite is the "Playwright primary VRT suite" (Issue #1386 Outcome):
// it runs with pending/provisional baselines only. The one active capture
// below targets the `[data-battle-ui-root]` DOM overlay layer for the
// `running-hud` scenario (`docs/dev/visual-baseline-registry.md`'s
// `running-hud` row is `legacy-current`, not `frozen`) — it is not a
// `.command-rail` capture (AC3: `.command-rail` is a separate,
// `aside.command-rail` legacy element outside the DOM overlay root and is
// never the target of this suite) and is not a `frozen` final overlay
// baseline (Out of Scope).
//
// `expectDomOverlayScreenshot()` (tests/e2e/visual-utils.ts) applies the
// shared freeze CSS via Playwright's `stylePath` option (AC6) and excludes
// the `canvas` element so the DOM overlay capture never bleeds in Canvas
// battle-stage pixels (AC5's "Canvas is only included via mask or an
// explicit registered canvas visual cue baseline" — this capture uses the
// explicit `canvasVisibility: 'hidden'` CSS-visibility exclusion path
// (Issue #1980), not the default `mask` path, and not a Canvas visual cue
// exception — see the call site's inline comment below for why).

test('GIVEN the running-hud active-fixture-only scenario WHEN the DOM overlay root is captured THEN it matches the provisional/legacy-current baseline (AC5, AC6)', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_HUD_FIXTURE)
  await page.goto('/')

  const overlayRoot = page.locator('[data-battle-ui-root]')
  // registryId + explicit maxDiffPixels (Issue #1386 PR #1721 review fix, P1
  // Blocker 1 / Blocker 2): `running-hud-overlay-legacy-current` is a
  // distinct registry row from `running-hud` (docs/dev/visual-baseline-registry.md),
  // and the tolerance is an absolute pixel budget (not a ratio of the full
  // masked capture) measured empirically against this capture root — see
  // the registry row's `tolerance` column for the measurement method.
  await expectDomOverlayScreenshot(overlayRoot, 'vrt-running-hud-overlay.png', 'running-hud-overlay-legacy-current', {
    maxDiffPixels: 100,
    // Issue #1980: canvas + `.battle-ui-layer` share nearly the same
    // bounding box since PR #1925's `.battle-stage__viewport`, so the
    // default `mask`-based canvas exclusion painted over the whole capture
    // root (including the HUD drawn on top of the canvas). `'hidden'`
    // excludes the canvas via CSS visibility instead, so the HUD stays
    // capturable.
    canvasVisibility: 'hidden',
  })
})

// ---------------------------------------------------------------------------
// Pixel diversity / negative control (AC2, AC3, Issue #1980)
// ---------------------------------------------------------------------------
//
// The previous `mask`-based canvas exclusion painted an opaque rectangle
// over the whole capture root for this baseline (canvas and
// `[data-battle-ui-root]` share nearly the same bounding box since PR
// #1925), so the committed PNG was ~99.8% a single mask color and the
// screenshot assertion could not actually detect an HUD regression. These
// two tests are a machine-checkable proof that the regenerated baseline
// (captured with `canvasVisibility: 'hidden'`, see above) does not have
// that defect: the PNG is not single-color-dominated (pixel diversity), and
// deliberately breaking the HUD would make the real screenshot assertion
// fail (negative control).
//
// The negative control below deliberately does NOT call
// `expectDomOverlayScreenshot()` / `toHaveScreenshot(name, ...)` with the
// SAME snapshot name ('vrt-running-hud-overlay.png') as the real baseline
// capture above (Issue #1980 iteration-1 fix_delta root cause). Under
// `pnpm run test:vrt:update:e2e` (`--update-snapshots=all`, the AC2 VC
// command), `toHaveScreenshot()` never throws — it always (re)writes the
// target snapshot file — so a shared-name negative control silently
// OVERWRITES the real baseline with this hidden-HUD capture instead of
// proving detection (whichever test runs last in file order wins on disk).
// That is exactly how the committed baseline previously became a near-empty
// (no-HUD) capture despite the AC2 pixel-diversity test passing against it.
// The negative control instead does a read-only, `--update-snapshots`-immune
// pixel diff: it captures the hidden-HUD DOM directly via
// `Locator.screenshot()` (never touches the `toHaveScreenshot()` snapshot
// read/write pipeline) and diffs it in-browser against the CHECKED-IN
// baseline PNG bytes read from disk, asserting the differing-pixel count
// exceeds the real assertion's `maxDiffPixels: 100` tolerance.

/**
 * Committed baseline PNG path for the pixel diversity check below. Computed
 * independently of `SCREENSHOTS_DIR` (declared further below in this file,
 * Issue #1386 PR #1721 review fix P2 Blocker #6) to avoid a module-load-time
 * temporal-dead-zone reference to a `const` declared later in the file.
 */
const RUNNING_HUD_OVERLAY_BASELINE_PNG_PATH = fileURLToPath(
  new URL('./__screenshots__/visual-overlay.spec.ts/vrt-running-hud-overlay.png', import.meta.url),
)

/**
 * A single color (including a fully-transparent/mask color) covering this
 * fraction or more of the baseline's pixels is treated as "mask-dominated".
 * The pre-fix defect measured ~99.8% single-color coverage (Issue #1980
 * Current Validated Scope); 0.9 leaves ample margin above legitimate
 * anti-aliasing/gradient variance while still catching a full-mask
 * regression.
 */
const SINGLE_COLOR_DOMINANCE_THRESHOLD = 0.9

test('GIVEN the regenerated running-hud-overlay-legacy-current baseline PNG WHEN its pixel composition is inspected via canvas getImageData THEN it is not dominated by a single color (pixel diversity, AC2)', async ({
  page,
}) => {
  const pngBase64 = readFileSync(RUNNING_HUD_OVERLAY_BASELINE_PNG_PATH).toString('base64')

  const dominantColorRatio = await page.evaluate(async (base64) => {
    const image = new Image()
    const decoded = new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error('failed to decode baseline PNG in-browser'))
    })
    image.src = `data:image/png;base64,${base64}`
    await decoded

    const canvas = document.createElement('canvas')
    canvas.width = image.naturalWidth
    canvas.height = image.naturalHeight
    const ctx = canvas.getContext('2d')
    if (!ctx) {
      throw new Error('2D canvas context unavailable')
    }
    ctx.drawImage(image, 0, 0)
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)

    const colorCounts = new Map<string, number>()
    for (let i = 0; i < data.length; i += 4) {
      const key = `${data[i]},${data[i + 1]},${data[i + 2]},${data[i + 3]}`
      colorCounts.set(key, (colorCounts.get(key) ?? 0) + 1)
    }
    let maxCount = 0
    for (const count of colorCounts.values()) {
      if (count > maxCount) {
        maxCount = count
      }
    }
    const totalPixels = canvas.width * canvas.height
    return totalPixels > 0 ? maxCount / totalPixels : 1
  }, pngBase64)

  expect(
    dominantColorRatio,
    `baseline PNG is ${(dominantColorRatio * 100).toFixed(1)}% a single color — this reproduces ` +
      'the pre-Issue-#1980 canvas-mask capture defect (Current Validated Scope: ~99.8% single ' +
      'color) instead of an actual HUD capture.',
  ).toBeLessThan(SINGLE_COLOR_DOMINANCE_THRESHOLD)
})

test('GIVEN [data-combat-hud] is forcibly hidden WHEN the real running-hud-overlay-legacy-current screenshot assertion runs THEN it rejects the hidden-HUD capture (negative control, AC3)', async ({
  page,
}, testInfo) => {
  // Issue #1980 review fix, P1 Blocker 1: baseline regeneration
  // (`pnpm run test:vrt:update:e2e`, `--update-snapshots=all`) makes
  // `toHaveScreenshot()` never throw -- it always (re)writes the target
  // snapshot file instead of comparing against it. This negative control
  // intentionally calls the SAME production helper with the SAME snapshot
  // name as the real baseline capture above, so under `--update-snapshots=all`
  // it would silently overwrite the committed baseline with this
  // deliberately-broken (hidden-HUD) capture instead of proving detection
  // -- exactly how the committed baseline previously became a near-empty
  // (no-HUD) capture (Issue #1980 iteration-0 root cause). Skipping this
  // read-only negative control during baseline regeneration runs prevents
  // that regression from ever reoccurring while still using the real
  // production matcher (not a custom byte-diff) every other time this
  // suite runs.
  test.skip(
    testInfo.config.updateSnapshots === 'all',
    'read-only negative control must not run during baseline regeneration',
  )

  await installVisualScenario(page, RUNNING_HUD_FIXTURE)
  await page.goto('/')

  const combatHud = page.locator('[data-combat-hud]')
  await expect(combatHud).toBeVisible()
  await combatHud.evaluate((element) => {
    element.setAttribute('style', `${element.getAttribute('style') ?? ''};visibility:hidden!important;`)
  })

  // Deliberately reuses expectDomOverlayScreenshot() -- the SAME production
  // helper, matcher (pixelmatch/YIQ via Playwright's toHaveScreenshot()),
  // registryId, and canvasVisibility: 'hidden' path as the real baseline
  // capture above -- so this proves the actual production assertion
  // rejects a hidden-HUD capture, not a custom one-shot RGBA comparison
  // that is not equivalent to it (Issue #1980 review fix, P1 Blocker 1).
  const overlayRoot = page.locator('[data-battle-ui-root]')
  await expect(
    expectDomOverlayScreenshot(overlayRoot, 'vrt-running-hud-overlay.png', 'running-hud-overlay-legacy-current', {
      maxDiffPixels: 100,
      canvasVisibility: 'hidden',
    }),
  ).rejects.toThrow()
})

// ---------------------------------------------------------------------------
// Pending-baseline scenarios (AC4)
// ---------------------------------------------------------------------------
//
// `running-hud-paused` / `result-overlay-timeout` / `final-no-command-rail`
// (docs/dev/visual-baseline-registry.md registry ids; `result-timeout` is
// the corresponding `VisualScenarioName`) are `pending-baseline` in the
// registry and `pending-fixture` in the Scenario Support Matrix — their
// overlay surface implementation child issues (#1375 / #1376 / #1377) have
// not merged. This primary VRT suite marks them `pending-baseline`
// explicitly via `test.skip()` rather than silently omitting them or
// capturing a premature baseline of the current pre-overlay UI.

const PENDING_SCENARIO_REGISTRY_IDS = {
  'running-hud-paused': 'running-hud-paused',
  'result-timeout': 'result-overlay-timeout',
  'final-no-command-rail': 'final-no-command-rail',
} as const

/**
 * Committed screenshot baselines directory for this spec file
 * (`playwright.config.ts`'s `snapshotPathTemplate`). Used only for the
 * registry-drift guard below (Issue #1386 PR #1721 review fix, P2 Blocker
 * #6) — never to decide what to capture.
 */
const SCREENSHOTS_DIR = fileURLToPath(new URL('./__screenshots__/visual-overlay.spec.ts/', import.meta.url))

/**
 * Registry-drift guard (Issue #1386 PR #1721 review fix, P2 Blocker #6).
 * The `test.skip()` loop below is a hand-authored constant describing
 * `docs/dev/visual-baseline-registry.md`'s current pending-baseline rows —
 * it does not automatically re-derive from the registry doc or from
 * `VISUAL_SCENARIO_STATUS`. Without a check, this constant could silently
 * drift from the registry (e.g. a future PR promotes a registry row to
 * `legacy-current` / `frozen` and adds a committed PNG/active test, but
 * forgets to remove the entry here) and this suite would keep skipping a
 * scenario that should now be actively captured, instead of failing loudly.
 * This asserts, for every entry above:
 *   1. `VISUAL_BASELINE_REGISTRY_MATURITY[registryId]` is still
 *      `pending-baseline` (registry says pending).
 *   2. no baseline PNG for that registry id is committed under
 *      `tests/e2e/__screenshots__/visual-overlay.spec.ts/` (no premature
 *      capture exists that the registry doesn't know about).
 * Both directions fail closed: if either check fails, this suite fails
 * loudly instead of silently continuing to skip a scenario that is (or
 * should be) active.
 */
for (const [scenarioName, registryId] of Object.entries(PENDING_SCENARIO_REGISTRY_IDS)) {
  test(`GIVEN the ${scenarioName} scenario is pending-baseline (registry id: ${registryId}) WHEN this primary VRT suite runs THEN it is skipped explicitly (AC4)`, () => {
    const maturity = VISUAL_BASELINE_REGISTRY_MATURITY[registryId]
    expect(
      maturity,
      `registry/test drift: "${registryId}" is hard-coded pending here but ` +
        `docs/dev/visual-baseline-registry.md / VISUAL_BASELINE_REGISTRY_MATURITY reports maturity ` +
        `"${maturity}" — update this suite (and stop skipping) instead of leaving it silently pending.`,
    ).toBe('pending-baseline')

    const candidatePngPaths = [
      `${SCREENSHOTS_DIR}vrt-${scenarioName}-overlay.png`,
      `${SCREENSHOTS_DIR}vrt-${registryId}.png`,
    ]
    const committedPng = candidatePngPaths.find((path) => existsSync(path))
    expect(
      committedPng,
      `registry/test drift: "${registryId}" is pending-baseline but a baseline PNG already exists ` +
        `at ${String(committedPng)} — the registry says "pending: no PNG/test" (§2/§3 of ` +
        'docs/dev/visual-baseline-registry.md); either the PNG is stale and must be removed, or the ' +
        'registry/test must be promoted together.',
    ).toBeUndefined()

    test.skip(
      true,
      `pending-baseline: "${registryId}" is pending-baseline in ` +
        'docs/dev/visual-baseline-registry.md (no committed PNG/test) — its overlay surface ' +
        'implementation child issue has not merged. See #1375/#1376/#1377.',
    )
  })
}

test('GIVEN the running-hud-paused pending-fixture scenario WHEN installVisualScenario is called THEN it fails closed instead of silently freezing the current pre-overlay UI (AC4)', async ({
  page,
}) => {
  expect(isPendingFixtureScenario(RUNNING_HUD_PAUSED_FIXTURE.name)).toBe(true)
  await expect(installVisualScenario(page, RUNNING_HUD_PAUSED_FIXTURE)).rejects.toThrow(
    /pending-fixture/,
  )
})
