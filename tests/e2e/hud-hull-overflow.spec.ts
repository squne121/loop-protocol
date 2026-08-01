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

/** Asserts `inner` is fully contained within `outer` expanded by `margin` on every side. */
function assertWithinSafeMargin(inner: Rect, outer: Rect, margin: number, label: string): void {
  expect(inner.x, `${label}: left edge inside safe margin`).toBeGreaterThanOrEqual(outer.x - margin)
  expect(inner.y, `${label}: top edge inside safe margin`).toBeGreaterThanOrEqual(outer.y - margin)
  expect(inner.x + inner.width, `${label}: right edge inside safe margin`).toBeLessThanOrEqual(
    outer.x + outer.width + margin,
  )
  expect(inner.y + inner.height, `${label}: bottom edge inside safe margin`).toBeLessThanOrEqual(
    outer.y + outer.height + margin,
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

test.describe('hud geometry: combat HUD stays inside the Canvas viewport (AC7)', () => {
  for (const vp of GEOMETRY_VIEWPORTS) {
    test(`viewport=${vp.label}: HUD is within canvas bounds, never overlaps header, Assist+Pause in viewport, no internal overflow`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height })
      await page.goto('/')
      await page.waitForSelector('[data-combat-hud]', { timeout: 10_000 })

      await assertHudGeometry(page, vp.label)
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
