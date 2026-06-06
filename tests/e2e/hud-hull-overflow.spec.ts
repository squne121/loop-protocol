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
