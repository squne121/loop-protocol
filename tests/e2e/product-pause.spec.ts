/**
 * E2E: product Pause/Resume surface (Issue #884)
 *
 * Covers AC17 behavioral assertions:
 * - HUD Pause button toggles aria-pressed (pause / resume cycle)
 * - Escape key toggles pause / resume
 * - P key pauses / resumes when canvas has focus
 * - P key is ignored when body or HUD button has focus (WCAG 2.1.4)
 * - visibilitychange hidden triggers auto-pause; visible does NOT auto-resume
 *
 * Test strategy:
 * - Navigate to app and wait for running phase via __LOOP_E2E__ hook.
 * - Observe HUD DOM (aria-pressed, aria-label, pause-status) to assert state.
 * - Trigger key events and visibility changes via page.keyboard / page.evaluate.
 *
 * NOTE: Automatic E2E results confirm integration correctness only.
 * Human playtesting remains required to evaluate UX.
 */

import { test, expect, type Page } from '@playwright/test'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface LoopE2EState {
  tick: number
  elapsedMs: number
  loopPhase:
    | 'title_menu'
    | 'load_menu'
    | 'preparation'
    | 'running'
    | 'result'
    | 'debrief_pending_reward'
    | 'debrief_reward_claimed'
}

async function getGameState(page: Page): Promise<LoopE2EState> {
  return page.evaluate(() => {
    const hook = (
      window as Window & {
        __LOOP_E2E__?: { getState: () => LoopE2EState }
      }
    ).__LOOP_E2E__
    if (!hook) {
      throw new Error('__LOOP_E2E__ hook not found. Was the app built with VITE_E2E_MODE=true?')
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return hook.getState() as any
  })
}

/** Wait for the game to reach the running phase. */
async function waitForRunning(page: Page): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.loopPhase
      },
      { timeout: 10_000, intervals: [100] },
    )
    .toBe('running')
}

/** Get the current aria-pressed value of the Pause button. */
async function getPauseButtonAriaPressed(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const btn = document.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    return btn ? btn.getAttribute('aria-pressed') : null
  })
}

/** Get the current textContent of the pause-status live region. */
async function getPauseStatusText(page: Page): Promise<string> {
  return page.evaluate(() => {
    const el = document.querySelector<HTMLElement>('[data-field="pause-status"]')
    return el ? (el.textContent ?? '') : ''
  })
}

/** Wait for aria-pressed to reach the expected value with polling. */
async function waitForAriaPressed(page: Page, expected: string): Promise<void> {
  await expect
    .poll(() => getPauseButtonAriaPressed(page), { timeout: 3_000, intervals: [50] })
    .toBe(expected)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await waitForRunning(page)
})

// ---------------------------------------------------------------------------
// AC17: HUD Pause button → aria-pressed toggle
// ---------------------------------------------------------------------------

test('GIVEN running phase WHEN Pause button clicked THEN aria-pressed becomes "true"', async ({
  page,
}) => {
  // Precondition: not paused
  await waitForAriaPressed(page, 'false')

  // Click pause button
  await page.click('[data-action="toggle-pause"]')

  // aria-pressed must flip to true
  await waitForAriaPressed(page, 'true')

  // pause-status live region should show "Paused"
  await expect
    .poll(() => getPauseStatusText(page), { timeout: 2_000 })
    .toBe('Paused')

  // Button textContent remains fixed (no switching between "Pause" / "Resume")
  const btnText = await page.evaluate(() => {
    const btn = document.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    return btn?.textContent ?? ''
  })
  expect(btnText).toBe('Pause')
})

test('GIVEN paused WHEN Pause button clicked again THEN aria-pressed becomes "false" (resume)', async ({
  page,
}) => {
  // Enter pause
  await page.click('[data-action="toggle-pause"]')
  await waitForAriaPressed(page, 'true')

  // Resume
  await page.click('[data-action="toggle-pause"]')
  await waitForAriaPressed(page, 'false')

  await expect.poll(() => getPauseStatusText(page), { timeout: 2_000 }).toBe('')
})

// ---------------------------------------------------------------------------
// AC17: Escape key pause / resume
// ---------------------------------------------------------------------------

test('GIVEN running phase WHEN Escape pressed THEN aria-pressed becomes "true" (pause)', async ({
  page,
}) => {
  await waitForAriaPressed(page, 'false')

  await page.keyboard.press('Escape')

  await waitForAriaPressed(page, 'true')
})

test('GIVEN paused WHEN Escape pressed THEN aria-pressed becomes "false" (resume)', async ({
  page,
}) => {
  // Pause first via Escape
  await page.keyboard.press('Escape')
  await waitForAriaPressed(page, 'true')

  // Resume via Escape
  await page.keyboard.press('Escape')
  await waitForAriaPressed(page, 'false')
})

// ---------------------------------------------------------------------------
// AC17: P key pause / resume when canvas has focus
// ---------------------------------------------------------------------------

test('GIVEN canvas focused WHEN P pressed THEN aria-pressed becomes "true" (pause)', async ({
  page,
}) => {
  await waitForAriaPressed(page, 'false')

  // Focus canvas
  await page.focus('.battle-stage__canvas')

  await page.keyboard.press('p')

  await waitForAriaPressed(page, 'true')
})

test('GIVEN paused with canvas focused WHEN P pressed THEN aria-pressed becomes "false" (resume)', async ({
  page,
}) => {
  // Focus canvas and pause
  await page.focus('.battle-stage__canvas')
  await page.keyboard.press('p')
  await waitForAriaPressed(page, 'true')

  // Resume with P
  await page.keyboard.press('p')
  await waitForAriaPressed(page, 'false')
})

// ---------------------------------------------------------------------------
// AC17: P key is ignored when body / HUD button has focus
// ---------------------------------------------------------------------------

test('GIVEN body has focus WHEN P pressed THEN aria-pressed stays "false" (P ignored)', async ({
  page,
}) => {
  await waitForAriaPressed(page, 'false')

  // Move focus away from canvas to body
  await page.evaluate(() => {
    ;(document.activeElement as HTMLElement | null)?.blur()
  })

  await page.keyboard.press('p')

  // Should remain unpaused
  // Wait a short moment for any potential state change
  await page.waitForTimeout(200)
  const ariaPressed = await getPauseButtonAriaPressed(page)
  expect(ariaPressed).toBe('false')
})

test('GIVEN HUD button has focus WHEN P pressed THEN aria-pressed stays "false" (P ignored)', async ({
  page,
}) => {
  await waitForAriaPressed(page, 'false')

  // Focus a HUD button (not canvas)
  await page.focus('[data-action="toggle-pause"]')

  await page.keyboard.press('p')

  await page.waitForTimeout(200)
  const ariaPressed = await getPauseButtonAriaPressed(page)
  expect(ariaPressed).toBe('false')
})

// ---------------------------------------------------------------------------
// AC17: visibilitychange hidden → auto-pause; visible → no auto-resume
// ---------------------------------------------------------------------------

test('GIVEN running phase WHEN visibilitychange hidden fired THEN aria-pressed becomes "true" (auto-pause)', async ({
  page,
}) => {
  await waitForAriaPressed(page, 'false')

  // Simulate tab/window hide
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true,
    })
    Object.defineProperty(document, 'hidden', {
      value: true,
      writable: true,
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })

  await waitForAriaPressed(page, 'true')
})

test('GIVEN auto-paused WHEN visibilitychange visible fired THEN aria-pressed stays "true" (no auto-resume)', async ({
  page,
}) => {
  // Auto-pause via visibilitychange hidden
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true,
    })
    Object.defineProperty(document, 'hidden', {
      value: true,
      writable: true,
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })

  await waitForAriaPressed(page, 'true')

  // Simulate tab/window becoming visible again
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true,
    })
    Object.defineProperty(document, 'hidden', {
      value: false,
      writable: true,
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
  })

  // Should remain paused (no auto-resume)
  await page.waitForTimeout(300)
  const ariaPressed = await getPauseButtonAriaPressed(page)
  expect(ariaPressed).toBe('true')
})
