/**
 * E2E: HUD Hull overflow prevention (Issue #693)
 *
 * Covers AC2a / AC2b / AC2c:
 * - AC2a: STATUS > HULL displays 99999/99999 and 999999999/999999999 without horizontal overflow
 * - AC2b: .stat-grid dd scrollWidth <= clientWidth across viewports 1280x720, 980x720, 800x720, 375x667
 * - AC2c: .stat-grid does not push the parent right rail / app shell wider than its container
 *
 * Test strategy:
 * - Override player hp / maxHp via window.__LOOP_TEST_OVERRIDE__ before page load
 * - Check DOM element scrollWidth <= clientWidth for each dd and the stat-grid container
 * - Check app-shell / right rail does not overflow body
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
    const hpEl = document.querySelector<HTMLElement>('[data-field="hp"]')
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
 * Check that .stat-grid container does not push parent wider than body.
 * Returns true if no overflow detected.
 */
async function measureAppShellOverflow(page: Page): Promise<{ overflowed: boolean; bodyW: number; appShellW: number }> {
  return page.evaluate(() => {
    const body = document.body
    const appShell = document.querySelector<HTMLElement>('.app-shell')
    const bodyW = body.scrollWidth
    const appShellW = appShell ? appShell.scrollWidth : 0
    // Allow 1px rounding tolerance
    return {
      overflowed: appShellW > bodyW + 1,
      bodyW,
      appShellW,
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
        // Wait for HUD to be rendered (data-field="hp" must be present)
        await page.waitForSelector('[data-field="hp"]', { timeout: 10_000 })

        // Inject large hull text (AC2a)
        await injectHullText(page, hullText)

        // Small settle time for layout recalculation
        await page.waitForTimeout(100)

        // AC2b: scrollWidth <= clientWidth for all .stat-grid dd
        const { overflowed, details } = await measureStatGridOverflow(page)
        expect(
          overflowed,
          `Overflow detected in viewport ${vp.label} with hull="${hullText}": ${details.join('; ')}`,
        ).toBe(false)

        // AC2c: app-shell not pushed wider than body
        const { overflowed: appOverflowed, bodyW, appShellW } = await measureAppShellOverflow(page)
        expect(
          appOverflowed,
          `app-shell pushed wider than body in viewport ${vp.label}: appShellW=${appShellW} bodyW=${bodyW}`,
        ).toBe(false)
      })
    }
  }
})
