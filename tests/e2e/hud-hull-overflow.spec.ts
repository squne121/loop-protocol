/**
 * E2E: HUD Hull overflow prevention (Issue #693)
 *
 * Covers AC2a / AC2b / AC2c:
 * - AC2a: STATUS > HULL displays 99999/99999 and 999999999/999999999 without horizontal overflow
 * - AC2b: .stat-grid dd scrollWidth <= clientWidth across viewports 1280x720, 980x720, 800x720, 375x667
 * - AC2c: .stat-grid does not push the parent right rail / app shell wider than its container
 *
 * Test strategy:
 * - Directly inject long Hull text into the rendered HUD element.
 *   This isolates CSS overflow behavior from game-state setup.
 * - Check DOM element scrollWidth <= clientWidth for each dd and the stat-grid container
 * - Check page-level horizontal overflow: document/body scrollWidth <= viewport clientWidth,
 *   and app-shell bounding rect stays within viewport bounds (AC2c)
 */

import { test, expect, type Page } from '@playwright/test'

// ---------------------------------------------------------------------------
// Helper: inject large HP values into the HUD via page.evaluate
// ---------------------------------------------------------------------------

/**
 * Force the HUD's Hull dd element to display a specific text,
 * bypassing the game loop. This simulates what the game would render
 * when player.hp / player.maxHp are large values.
 */
async function injectHullText(page: Page, text: string): Promise<void> {
  await page.evaluate((hullText) => {
    // Scope Delta (Issue #1375): the Hull field moved from
    // `[data-field="hp"]` (all phases) to `[data-field="combat-hud-hull"]`
    // inside the running-only `data-combat-hud` root.
    const hpEl = document.querySelector<HTMLElement>('[data-field="combat-hud-hull"]')
    if (hpEl) {
      hpEl.textContent = hullText
    }
  }, text)
}

/**
 * Measure overflow for .stat-grid dd elements.
 * Returns { overflowed: boolean, details: string[] }
 */
async function measureStatGridOverflow(
  page: Page,
): Promise<{ overflowed: boolean; details: string[] }> {
  return page.evaluate(() => {
    const dds = Array.from(document.querySelectorAll<HTMLElement>('.stat-grid dd'))
    const details: string[] = []
    let overflowed = false

    for (const dd of dds) {
      const scrollW = dd.scrollWidth
      const clientW = dd.clientWidth
      if (scrollW > clientW) {
        overflowed = true
        details.push(
          `dd[data-field="${dd.dataset.field ?? '?'}"] scrollWidth=${scrollW} > clientWidth=${clientW}`,
        )
      }
    }

    return { overflowed, details }
  })
}

/**
 * Measure page-level horizontal overflow for AC2c.
 * Checks both document/body scrollWidth vs viewport clientWidth,
 * and app-shell bounding rect vs viewport bounds.
 * body.scrollWidth includes overflow content, so the reference must be
 * document.documentElement.clientWidth (the actual viewport inner width).
 */
async function measurePageHorizontalOverflow(
  page: Page,
): Promise<{
  overflowed: boolean
  viewportW: number
  docScrollW: number
  bodyScrollW: number
  appShellLeft: number | null
  appShellRight: number | null
}> {
  return page.evaluate(() => {
    const doc = document.documentElement
    const body = document.body
    const appShell = document.querySelector<HTMLElement>('.app-shell')
    const rect = appShell?.getBoundingClientRect()

    const viewportW = doc.clientWidth
    const docScrollW = doc.scrollWidth
    const bodyScrollW = body.scrollWidth

    // Page has horizontal overflow if content is wider than viewport
    const pageOverflowed = Math.max(docScrollW, bodyScrollW) > viewportW + 1
    // App-shell escapes viewport if its rect exceeds viewport bounds
    const shellEscapedViewport = rect != null && (rect.left < -1 || rect.right > viewportW + 1)

    return {
      overflowed: pageOverflowed || shellEscapedViewport,
      viewportW,
      docScrollW,
      bodyScrollW,
      appShellLeft: rect?.left ?? null,
      appShellRight: rect?.right ?? null,
    }
  })
}

// ---------------------------------------------------------------------------
// Test matrix: viewports × hull values
// ---------------------------------------------------------------------------

const VIEWPORTS = [
  { width: 1280, height: 720, label: '1280x720' },
  { width: 980, height: 720, label: '980x720' },
  { width: 800, height: 720, label: '800x720' },
  { width: 375, height: 667, label: '375x667' },
]

const HULL_VALUES = [
  '99999/99999',
  '999999999/999999999',
]

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

// Issue #1375 PR #1925 review (owner playtest, P0-3): the AC7 desktop
// resolutions from the Issue body were never covered by an automated
// viewport matrix. Added alongside the existing AC2 overflow matrix.
const GEOMETRY_VIEWPORTS = [
  ...VIEWPORTS,
  { width: 1366, height: 768, label: '1366x768' },
  { width: 1920, height: 1080, label: '1920x1080' },
]

test.describe('hud overflow: stat-grid dd does not overflow in any viewport', () => {
  for (const vp of VIEWPORTS) {
    for (const hullText of HULL_VALUES) {
      test(`hull="${hullText}" viewport=${vp.label}: scrollWidth <= clientWidth`, async ({
        page,
      }) => {
        // Set viewport before navigation
        await page.setViewportSize({ width: vp.width, height: vp.height })

        // Navigate to app
        await page.goto('/')
        // Wait for HUD to be rendered (data-field="combat-hud-hull" must be present)
        await page.waitForSelector('[data-field="combat-hud-hull"]', { timeout: 10_000 })

        // Inject large hull text (AC2a)
        await injectHullText(page, hullText)

        // AC2b: scrollWidth <= clientWidth for all .stat-grid dd
        // Use expect.poll to retry until layout settles (avoids fixed sleep flakiness)
        await expect
          .poll(
            async () => {
              const r = await measureStatGridOverflow(page)
              return r
            },
            { message: `stat-grid dd should not overflow in viewport ${vp.label} hull="${hullText}"` },
          )
          .toMatchObject({ overflowed: false })

        // AC2c: page has no horizontal overflow; app-shell stays within viewport
        const shell = await measurePageHorizontalOverflow(page)
        expect(
          shell.overflowed,
          `horizontal overflow in viewport ${vp.label} hull="${hullText}": ` +
            `viewportW=${shell.viewportW} docScroll=${shell.docScrollW} bodyScroll=${shell.bodyScrollW} ` +
            `appShell=[${shell.appShellLeft}, ${shell.appShellRight}]`,
        ).toBe(false)
      })
    }
  }
})

// ---------------------------------------------------------------------------
// AC7 (Issue #1375 PR #1925 review, owner playtest, P0-3): the combat HUD's
// bounding box must stay inside the Canvas viewport (not the header) with a
// 16px safe margin, never overlap the header rect, and both Assist allies
// and Pause must stay in the viewport together, across desktop resolutions.
// ---------------------------------------------------------------------------

const HUD_SAFE_MARGIN_PX = 16

interface Rect {
  x: number
  y: number
  width: number
  height: number
}

function rectsIntersect(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
}

/**
 * Asserts `inner` is fully contained within `outer` INSET by `margin` on
 * every side (Issue #1958 AC3 fix): `inner` must stay at least `margin`
 * CSS px inside `outer`'s edges, not merely within `outer` expanded
 * outward by `margin` (the previous, backwards, false-green comparison --
 * that version allowed `inner` to overflow `outer` by up to `margin` px
 * and still pass). `epsilon` absorbs sub-pixel rounding only, never the
 * declared safe-zone margin itself.
 */
function assertWithinSafeMargin(inner: Rect, outer: Rect, margin: number, label: string): void {
  const epsilon = 1
  expect(inner.x, `${label}: left edge inside safe margin`).toBeGreaterThanOrEqual(
    outer.x + margin - epsilon,
  )
  expect(inner.y, `${label}: top edge inside safe margin`).toBeGreaterThanOrEqual(
    outer.y + margin - epsilon,
  )
  expect(inner.x + inner.width, `${label}: right edge inside safe margin`).toBeLessThanOrEqual(
    outer.x + outer.width - margin + epsilon,
  )
  expect(inner.y + inner.height, `${label}: bottom edge inside safe margin`).toBeLessThanOrEqual(
    outer.y + outer.height - margin + epsilon,
  )
}

async function assertHudGeometry(page: Page, label: string): Promise<void> {
  const hud = page.locator('[data-combat-hud]')
  const canvas = page.locator('canvas.battle-stage__canvas')
  const header = page.locator('.battle-stage__header')

  await expect(hud).toBeVisible()

  const hudBox = await hud.boundingBox()
  const canvasBox = await canvas.boundingBox()
  const headerBox = await header.boundingBox()

  expect(hudBox, `${label}: combat HUD must have a bounding box`).not.toBeNull()
  expect(canvasBox, `${label}: canvas must have a bounding box`).not.toBeNull()
  expect(headerBox, `${label}: header must have a bounding box`).not.toBeNull()

  if (!hudBox || !canvasBox || !headerBox) {
    return
  }

  // HUD is within the Canvas viewport (16px safe margin), not the header
  // (P0-1: the HUD's containing block is `.battle-stage__viewport`, whose
  // bounds match the canvas).
  assertWithinSafeMargin(hudBox, canvasBox, HUD_SAFE_MARGIN_PX, `${label} HUD-in-canvas`)

  // HUD and header never overlap as rectangles.
  expect(
    rectsIntersect(hudBox, headerBox),
    `${label}: HUD box ${JSON.stringify(hudBox)} must not intersect header box ${JSON.stringify(headerBox)}`,
  ).toBe(false)

  // Assist allies and Pause both stay in the viewport together.
  await expect(page.getByRole('button', { name: 'Assist allies' })).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Pause' })).toBeInViewport()

  // HUD element itself has no internal overflow (neither axis).
  const overflow = await hud.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }))
  expect(overflow.scrollWidth, `${label}: HUD scrollWidth <= clientWidth`).toBeLessThanOrEqual(
    overflow.clientWidth,
  )
  expect(overflow.scrollHeight, `${label}: HUD scrollHeight <= clientHeight`).toBeLessThanOrEqual(
    overflow.clientHeight,
  )
}

/**
 * Variant of `assertHudGeometry` for the 375x667 minimum supported
 * viewport (Issue #1958 AC1/AC2): Weapon/Assist (`edge-control`) is
 * intentionally collapsed/hidden by `src/style.css`'s
 * `@media (max-width: 420px)` rule (the semantic state table's collapse
 * priority), so "Assist allies in viewport" is not asserted here. Hull /
 * critical / Kills / elapsed / Pause -- the never-collapsing fragments --
 * still must stay within the Canvas safe margin, never overlap the header,
 * and the HUD must have no internal overflow.
 */
async function assertHudGeometryAtCollapsedMinimum(page: Page, label: string): Promise<void> {
  const hud = page.locator('[data-combat-hud]')
  const canvas = page.locator('canvas.battle-stage__canvas')
  const header = page.locator('.battle-stage__header')

  await expect(hud).toBeVisible()

  const hudBox = await hud.boundingBox()
  const canvasBox = await canvas.boundingBox()
  const headerBox = await header.boundingBox()

  expect(hudBox, `${label}: combat HUD must have a bounding box`).not.toBeNull()
  expect(canvasBox, `${label}: canvas must have a bounding box`).not.toBeNull()
  expect(headerBox, `${label}: header must have a bounding box`).not.toBeNull()

  if (!hudBox || !canvasBox || !headerBox) {
    return
  }

  assertWithinSafeMargin(hudBox, canvasBox, HUD_SAFE_MARGIN_PX, `${label} HUD-in-canvas`)

  expect(
    rectsIntersect(hudBox, headerBox),
    `${label}: HUD box ${JSON.stringify(hudBox)} must not intersect header box ${JSON.stringify(headerBox)}`,
  ).toBe(false)

  // AC1, AC2: the never-collapsing fragments stay reachable even when
  // edge-control is collapsed.
  await expect(page.getByRole('button', { name: 'Pause' })).toBeInViewport()

  const overflow = await hud.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }))
  expect(overflow.scrollWidth, `${label}: HUD scrollWidth <= clientWidth`).toBeLessThanOrEqual(
    overflow.clientWidth,
  )
  expect(overflow.scrollHeight, `${label}: HUD scrollHeight <= clientHeight`).toBeLessThanOrEqual(
    overflow.clientHeight,
  )
}

test.describe('hud geometry: combat HUD stays inside the Canvas viewport (AC7)', () => {
  for (const vp of GEOMETRY_VIEWPORTS) {
    // Issue #1958 AC2: the combat HUD's placement/safe-zone/priority
    // redesign (`src/ui/combatHud.ts`'s zoned grid + `src/style.css`'s
    // `@media (max-width: 420px)` progressive-disclosure collapse of the
    // edge-control zone) fits the 375x667 minimum supported viewport
    // (`Supported minimum viewport` = 375x667) within the strict `inner`
    // safe-margin containment fixed by AC3's `assertWithinSafeMargin`
    // rewrite. The `test.fixme` deferred by Issue #1956 is now resolved.
    test(`viewport=${vp.label}: HUD is within canvas bounds, never overlaps header, Assist+Pause in viewport, no internal overflow`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height })
      await page.goto('/')
      await page.waitForSelector('[data-combat-hud]', { timeout: 10_000 })

      // AC2: at 375x667, Weapon/Assist collapse first (semantic state table
      // collapse priority) -- Assist allies is not required to stay in
      // viewport at the minimum supported width, so this asserts a variant
      // of `assertHudGeometry` without that check.
      if (vp.label === '375x667') {
        await assertHudGeometryAtCollapsedMinimum(page, vp.label)
      } else {
        await assertHudGeometry(page, vp.label)
      }
    })
  }
})

// ---------------------------------------------------------------------------
// Regression case for the owner's actual playtest report: Windows/Chrome,
// viewport 1437x1365, devicePixelRatio ~= 0.667 (effectively a wider/taller
// *logical* viewport than the physical display, e.g. OS display scaling
// below 100%). Playwright cannot emulate OS-level zoom directly, but
// `deviceScaleFactor` is the closest supported lever and can only be set at
// browser-context creation, so this test opens its own context rather than
// reusing the default `page` fixture.
//
// Issue #1375 PR #1925 owner playtest (P1, iteration 4): this is an
// approximation, not a substitute for real evidence. Chromium's
// `deviceScaleFactor` models device pixel ratio, which is a different
// mechanism from a real browser's page zoom or the OS's display-scaling
// setting the owner actually reported (Windows Chrome, ~150% logical vs.
// physical mismatch). A regression this test misses because the two
// mechanisms diverge in some edge case would not be caught here — real
// browser zoom / OS scaling still needs to be re-checked against actual
// hardware/browser combinations when in doubt, this test only guards
// against DPR-shaped layout regressions in CI.
// ---------------------------------------------------------------------------

test.describe('hud geometry: low-DPR regression (owner playtest report)', () => {
  test('viewport=1437x1365 deviceScaleFactor=0.667 (approx): HUD stays within canvas bounds', async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width: 1437, height: 1365 },
      // Chromium requires deviceScaleFactor > 0; 0.667 approximates the
      // owner's report of an effectively higher-resolution logical viewport
      // than the physical display (DPR below 1).
      deviceScaleFactor: 0.667,
    })
    const page = await context.newPage()

    try {
      await page.goto('/')
      await page.waitForSelector('[data-combat-hud]', { timeout: 10_000 })

      await assertHudGeometry(page, 'DPR~0.667 1437x1365')
    } finally {
      await context.close()
    }
  })
})

// ---------------------------------------------------------------------------
// Owner playtest regression (PR #1925 owner comment
// https://github.com/squne121/loop-protocol/pull/1925#issuecomment-5151416762,
// iteration 4, P0): "defeated and could not proceed". Root cause: the
// `.legacy-result-surface`'s three `.panel` rows (Sortie / Pilot updates /
// actions=Return to hangar etc.) could overflow `.battle-stage__viewport`'s
// clipped Canvas height, pushing `[data-action="confirm-result"]` below the
// fold with no visible affordance that `.battle-hud-layer`'s
// `overflow-y: auto` scroll was required.
//
// Reaches the result phase through the SAME deterministic fixture
// `tests/e2e/m2-combat-mvp.spec.ts` already uses for its defeat coverage
// (`__E2E_PLAYER_HP_OVERRIDE__ = 1`: first enemy contact ends the sortie in
// defeat) — never a DOM/state shortcut into the result phase. Deliberately
// does NOT call `confirmButton.click()`: Playwright auto-scrolls a click
// target into view first, which is exactly the behavior that let this bug
// go undetected by `tests/e2e/m3-loop-mvp.spec.ts` (Issue #1375 Allowed
// Paths does not include that file, so it is not modified here). Instead
// this asserts the button's actual rendered geometry directly.
// ---------------------------------------------------------------------------

const RESULT_ACTION_VIEWPORTS = [
  { width: 1280, height: 720, label: '1280x720' },
  { width: 1366, height: 768, label: '1366x768' },
  { width: 1920, height: 1080, label: '1920x1080' },
  { width: 1437, height: 1365, label: '1437x1365' },
  { width: 956, height: 1032, label: '956x1032' },
]

async function getSortieStatus(page: Page): Promise<string> {
  return page.evaluate(() => {
    const hook = (
      window as Window & {
        __LOOP_E2E__?: { getState: () => { sortie: { status: string } } }
      }
    ).__LOOP_E2E__
    if (!hook) {
      throw new Error('__LOOP_E2E__ hook not found. Was the app built with VITE_E2E_MODE=true?')
    }
    return hook.getState().sortie.status
  })
}

/**
 * Asserts `inner` is fully contained within `outer` with no safe margin
 * (unlike `assertWithinSafeMargin()` above): the button must not be clipped
 * by `.battle-stage__viewport`'s `overflow: hidden`, so any edge escaping
 * `outer`'s bounds means the button is (at least partially) not actually
 * reachable/visible to the player. `epsilon` absorbs sub-pixel rounding
 * only.
 */
function assertFullyContained(inner: Rect, outer: Rect, label: string): void {
  const epsilon = 1
  expect(inner.x, `${label}: left edge inside canvas viewport`).toBeGreaterThanOrEqual(
    outer.x - epsilon,
  )
  expect(inner.y, `${label}: top edge inside canvas viewport`).toBeGreaterThanOrEqual(
    outer.y - epsilon,
  )
  expect(inner.x + inner.width, `${label}: right edge inside canvas viewport`).toBeLessThanOrEqual(
    outer.x + outer.width + epsilon,
  )
  expect(inner.y + inner.height, `${label}: bottom edge inside canvas viewport`).toBeLessThanOrEqual(
    outer.y + outer.height + epsilon,
  )
}

test.describe('hud result action geometry: confirm-result stays reachable after defeat (owner playtest regression)', () => {
  for (const vp of RESULT_ACTION_VIEWPORTS) {
    test(`viewport=${vp.label}: Return to hangar is in the browser viewport and inside the Canvas viewport rect after defeat`, async ({
      page,
    }) => {
      test.setTimeout(30_000)

      // Deterministic defeat fixture (same mechanism as
      // tests/e2e/m2-combat-mvp.spec.ts's existing defeat coverage): 1 HP
      // means the first enemy contact ends the sortie in defeat.
      await page.addInitScript(() => {
        ;(
          window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }
        ).__E2E_PLAYER_HP_OVERRIDE__ = 1
      })
      await page.setViewportSize({ width: vp.width, height: vp.height })
      await page.goto('/')

      await expect
        .poll(async () => getSortieStatus(page), { timeout: 25_000, intervals: [200] })
        .toBe('defeat')

      const confirmButton = page.locator('[data-action="confirm-result"]')
      const viewport = page.locator('.battle-stage__viewport')

      // Do NOT call confirmButton.click() here — Playwright's auto-scroll-
      // into-view before clicking would silently paper over exactly the bug
      // this test exists to catch.
      await expect(confirmButton).toBeInViewport()

      const buttonBox = await confirmButton.boundingBox()
      const viewportBox = await viewport.boundingBox()
      expect(buttonBox, `${vp.label}: confirm-result must have a bounding box`).not.toBeNull()
      expect(
        viewportBox,
        `${vp.label}: .battle-stage__viewport must have a bounding box`,
      ).not.toBeNull()
      if (!buttonBox || !viewportBox) {
        return
      }

      assertFullyContained(buttonBox, viewportBox, `${vp.label} confirm-result-in-canvas-viewport`)
    })
  }
})

// ---------------------------------------------------------------------------
// Issue #1376 AC11: browser zoom 200% -- Playwright has no native "page zoom"
// control, so this approximates it the same way the codebase already
// documents for OS display scaling (see the low-DPR describe block above):
// Chromium supports the non-standard CSS `zoom` property, applied to the
// root element, as the closest available lever. Not a substitute for real
// browser zoom on actual hardware, but guards against zoom-shaped layout
// regressions in CI (result heading and Return to hangar must stay inside
// the browser viewport and fully reachable).
// ---------------------------------------------------------------------------

test.describe('hud result action geometry: 200% browser zoom (AC11, Issue #1376)', () => {
  // Playwright has no native "real browser page zoom" control (well-known
  // limitation -- Chrome's Ctrl+ zoom is not exposed via CDP for this
  // purpose). This approximates it the same way the low-DPR describe block
  // above already documents for OS display scaling: the CSS `zoom` property
  // (Chromium-only, non-standard) applied to the root element scales
  // rendered content within the SAME layout viewport, which is directionally
  // similar to how real browser zoom shrinks the effective CSS-pixel space
  // available for layout. It is NOT a substitute for testing against real
  // browser zoom on actual hardware. Because this approximation is more
  // aggressive than real zoom typically is in practice, the assertion here
  // is scroll-reachability (matches Playwright's own `click()` semantics,
  // which auto-scrolls the target into view) rather than strict
  // "no-scroll-required" viewport containment -- a scrollable
  // `.battle-screen-layer` with `overflow-y: auto` is an acceptable
  // affordance at this extreme zoom level, unlike being clipped/hidden
  // entirely.
  test('viewport=1280x720 zoom=200%: result heading is visible and Return to hangar remains scroll-reachable and clickable after defeat', async ({
    page,
  }) => {
    test.setTimeout(30_000)

    await page.addInitScript(() => {
      ;(window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }).__E2E_PLAYER_HP_OVERRIDE__ = 1
    })
    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto('/')

    await expect
      .poll(async () => getSortieStatus(page), { timeout: 25_000, intervals: [200] })
      .toBe('defeat')

    // Approximate 200% browser zoom via the CSS `zoom` property.
    await page.evaluate(() => {
      document.documentElement.style.zoom = '2'
    })

    const resultHeading = page.locator('#phase-screen-result-heading')
    const confirmButton = page.locator('[data-action="confirm-result"]')

    await expect(resultHeading).toBeVisible({ timeout: 5_000 })
    // AC11: Return to hangar must not be clipped/hidden -- it must still be
    // reachable (scroll-into-view + click) and actually invoke the action.
    await confirmButton.scrollIntoViewIfNeeded()
    await expect(confirmButton).toBeVisible()
    await confirmButton.click()
    await expect
      .poll(
        async () =>
          page.evaluate(() => {
            const hook = (
              window as Window & { __LOOP_E2E__?: { getState: () => { loopPhase: string } } }
            ).__LOOP_E2E__
            return hook ? hook.getState().loopPhase : 'no-hook'
          }),
        { timeout: 5_000, intervals: [50] },
      )
      .toBe('preparation')
  })
})
