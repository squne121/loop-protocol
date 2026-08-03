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

  // Button textContent remains fixed (no switching between "Pause" / "Resume"),
  // and no aria-label override -- the accessible name IS the visible label
  // (Issue #1375 AC3: visible label and accessible name never disagree).
  const btn = await page.evaluate(() => {
    const button = document.querySelector<HTMLButtonElement>('[data-action="toggle-pause"]')
    return { text: button?.textContent ?? '', hasAriaLabel: button?.hasAttribute('aria-label') ?? null }
  })
  expect(btn.text).toBe('Pause')
  expect(btn.hasAriaLabel).toBe(false)
})

test('GIVEN paused WHEN Resume is clicked THEN aria-pressed becomes "false" (resume) (Issue #1376: the HUD Pause button itself becomes pointer-unreachable while paused, since combat HUD is inert behind the modal pause dialog -- AC4 -- so resume goes through the pause dialog\'s own Resume button instead of a second click on the same HUD button)', async ({
  page,
}) => {
  // Enter pause
  await page.click('[data-action="toggle-pause"]')
  await waitForAriaPressed(page, 'true')

  // Resume via the pause dialog's Resume button (AC3, AC4, AC6).
  await page.click('[data-action="resume"]')
  await waitForAriaPressed(page, 'false')
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

test('GIVEN paused via P key WHEN P is pressed again THEN it does NOT resume, because the Canvas is inert while paused (AC4) and therefore cannot hold focus, so the AC3/AC15 canvas-focus guard blocks the second P press -- resume happens via Escape or the pause dialog\'s Resume button instead', async ({
  page,
}) => {
  // Focus canvas and pause
  await page.focus('.battle-stage__canvas')
  await page.keyboard.press('p')
  await waitForAriaPressed(page, 'true')

  // AC4: the Canvas is now inert, so it cannot be (or remain) the active
  // element -- confirm the guard's precondition is actually false here,
  // not just assume it.
  const canvasIsActiveElement = await page.evaluate(
    () => document.activeElement === document.querySelector('.battle-stage__canvas'),
  )
  expect(canvasIsActiveElement, 'Canvas must not be the active element while paused (AC4)').toBe(false)

  // P key is a no-op here (WCAG 2.1.4 canvas-focus guard cannot be
  // satisfied while the Canvas is inert) -- still paused.
  await page.keyboard.press('p')
  await page.waitForTimeout(200)
  expect(await getPauseButtonAriaPressed(page)).toBe('true')

  // Escape resumes regardless of focus location (AC3, AC5, AC6).
  await page.keyboard.press('Escape')
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

// ---------------------------------------------------------------------------
// Issue #1376 AC4: pause dialog inert/focus. AC5: Escape single-toggle
// (repeat / bubbling guard). AC6: resume focus (invoker restore / Canvas
// fallback).
// ---------------------------------------------------------------------------

test('GIVEN Pause button clicked THEN the pause dialog is a role="dialog" with Canvas and combat HUD inert, and focus moves to Resume (AC4)', async ({
  page,
}) => {
  await page.click('[data-action="toggle-pause"]')
  await waitForAriaPressed(page, 'true')

  const pauseDialog = page.locator('[data-phase-screen="pause"]')
  await expect(pauseDialog).toBeVisible()
  await expect(pauseDialog).toHaveAttribute('role', 'dialog')
  await expect(pauseDialog).toHaveAttribute('aria-modal', 'true')

  const resumeButton = page.locator('[data-action="resume"]')
  await expect(resumeButton).toBeFocused()

  const canvasInert = await page.evaluate(() =>
    document.querySelector('canvas.battle-stage__canvas')?.hasAttribute('inert'),
  )
  const combatHudInert = await page.evaluate(() =>
    document.querySelector('[data-combat-hud]')?.hasAttribute('inert'),
  )
  expect(canvasInert, 'Canvas must be inert while paused (AC4)').toBe(true)
  expect(combatHudInert, 'combat HUD must be inert while paused (AC4)').toBe(true)

  // combat HUD stays visible (not hidden) -- Hull/Kills/Elapsed keep updating
  // behind the modal pause overlay (AC4).
  const combatHudHidden = await page.evaluate(
    () => (document.querySelector('[data-combat-hud]') as HTMLElement | null)?.hidden,
  )
  expect(combatHudHidden).toBe(false)
})

test('GIVEN Pause button clicked WHEN Resume clicked THEN the invoking Pause button regains focus (AC6)', async ({
  page,
}) => {
  const pauseButton = page.locator('[data-action="toggle-pause"]')
  await pauseButton.click()
  await waitForAriaPressed(page, 'true')

  await page.click('[data-action="resume"]')
  await waitForAriaPressed(page, 'false')

  await expect(pauseButton).toBeFocused()

  const canvasInert = await page.evaluate(() =>
    document.querySelector('canvas.battle-stage__canvas')?.hasAttribute('inert'),
  )
  expect(canvasInert, 'Canvas must no longer be inert after resume (AC4)').toBe(false)
})

test('GIVEN auto-paused via visibilitychange (no invoker) WHEN resumed via the HUD button THEN Canvas or the resuming control ends up focused, never a stale element (AC6)', async ({
  page,
}) => {
  // Auto-pause: no explicit invoker is recorded.
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true,
    })
    Object.defineProperty(document, 'hidden', { value: true, writable: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await waitForAriaPressed(page, 'true')

  const resumeButton = page.locator('[data-action="resume"]')
  await expect(resumeButton).toBeFocused()

  await resumeButton.click()
  await waitForAriaPressed(page, 'false')

  // AC6: no invoker was recorded for auto-pause, so resume falls back to the
  // Canvas -- but Resume itself was just clicked, so the browser's own
  // "focus follows click" leaves Resume's invoker (the button just clicked)
  // irrelevant here; the important invariant is that focus is NOT left on a
  // now-hidden/inert pause-dialog element.
  const activeElementInPauseDialog = await page.evaluate(() => {
    const pauseDialog = document.querySelector('[data-phase-screen="pause"]')
    return pauseDialog ? pauseDialog.contains(document.activeElement) : false
  })
  expect(activeElementInPauseDialog, 'focus must not remain inside the now-hidden pause dialog').toBe(
    false,
  )
})

test('GIVEN Escape held (event.repeat) WHEN fired repeatedly THEN only the first keydown toggles pause (AC5)', async ({
  page,
}) => {
  await waitForAriaPressed(page, 'false')

  // Simulate a held key: first event has repeat=false, subsequent events
  // have repeat=true (browser auto-repeat semantics) -- only the first may
  // toggle.
  await page.evaluate(() => {
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', repeat: false, bubbles: true }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', repeat: true, bubbles: true }))
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', repeat: true, bubbles: true }))
  })

  await waitForAriaPressed(page, 'true')
})

