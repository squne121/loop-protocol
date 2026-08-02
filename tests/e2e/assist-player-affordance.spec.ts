import { expect, test, type Page } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

interface LoopE2EState {
  loopPhase:
    | 'title_menu'
    | 'load_menu'
    | 'preparation'
    | 'running'
    | 'result'
    | 'debrief_pending_reward'
    | 'debrief_reward_claimed'
  allies: Array<{
    id: number
    x: number
    y: number
    targetEntityId: string | null
    behaviorState: string
  }>
  enemies: Array<{
    id: number
    defeatedAtTick: number | null
  }>
  commandIntent: {
    activeIntent: 'none' | 'assist_player'
    bufferedIntentExpiresAtTick: number | null
  }
  arena: {
    width: number
    height: number
  }
  player: {
    aimX: number
    aimY: number
  }
}

type Scenario = {
  viewport: { width: number; height: number; label: string }
  zoom: { factor: number; label: string }
}

type EvidenceEntry = {
  viewport: string
  browser_zoom: string
  observed_devicePixelRatio: number
  observed_visualViewportScale: number | null
  userAgent: string
  screenshot_path: string
  canvas_css: { width: number; height: number }
  canvas_backing_store: { width: number; height: number }
  logical_arena: { width: number; height: number }
}

const SCENARIOS: Scenario[] = [
  { viewport: { width: 1280, height: 720, label: '1280x720' }, zoom: { factor: 1, label: '100%' } },
  { viewport: { width: 1280, height: 720, label: '1280x720' }, zoom: { factor: 1.25, label: '125%' } },
  { viewport: { width: 1280, height: 720, label: '1280x720' }, zoom: { factor: 1.5, label: '150%' } },
  { viewport: { width: 1280, height: 720, label: '1280x720' }, zoom: { factor: 2, label: '200%' } },
  { viewport: { width: 1366, height: 768, label: '1366x768' }, zoom: { factor: 1, label: '100%' } },
  { viewport: { width: 1366, height: 768, label: '1366x768' }, zoom: { factor: 1.25, label: '125%' } },
  { viewport: { width: 1366, height: 768, label: '1366x768' }, zoom: { factor: 1.5, label: '150%' } },
  { viewport: { width: 1366, height: 768, label: '1366x768' }, zoom: { factor: 2, label: '200%' } },
  { viewport: { width: 1920, height: 1080, label: '1920x1080' }, zoom: { factor: 1, label: '100%' } },
  { viewport: { width: 1920, height: 1080, label: '1920x1080' }, zoom: { factor: 1.25, label: '125%' } },
  { viewport: { width: 1920, height: 1080, label: '1920x1080' }, zoom: { factor: 1.5, label: '150%' } },
  { viewport: { width: 1920, height: 1080, label: '1920x1080' }, zoom: { factor: 2, label: '200%' } },
]

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

    return hook.getState()
  })
}

async function waitForRunningWithCombatActors(page: Page): Promise<void> {
  await expect
    .poll(async () => {
      const state = await getGameState(page)
      return {
        loopPhase: state.loopPhase,
        allies: state.allies.length,
        livingEnemies: state.enemies.filter((enemy) => enemy.defeatedAtTick === null).length,
      }
    }, { timeout: 10_000, intervals: [100] })
    .toEqual({
      loopPhase: 'running',
      allies: 1,
      livingEnemies: 1,
    })
}

async function applyBrowserZoom(page: Page, factor: number): Promise<void> {
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setPageScaleFactor', {
    pageScaleFactor: factor,
  })
}

async function collectEvidence(page: Page): Promise<Omit<EvidenceEntry, 'viewport' | 'browser_zoom' | 'screenshot_path'>> {
  return page.evaluate(() => {
    const canvas = document.querySelector<HTMLCanvasElement>('canvas.battle-stage__canvas')
    if (!canvas) {
      throw new Error('battle canvas not found')
    }

    const rect = canvas.getBoundingClientRect()
    const hook = (window as Window & { __LOOP_E2E__?: { getState: () => LoopE2EState } }).__LOOP_E2E__
    if (!hook) {
      throw new Error('__LOOP_E2E__ hook not found')
    }
    const state = hook.getState()
    return {
      observed_devicePixelRatio: window.devicePixelRatio ?? 1,
      observed_visualViewportScale: window.visualViewport?.scale ?? null,
      userAgent: navigator.userAgent,
      canvas_css: {
        width: rect.width,
        height: rect.height,
      },
      canvas_backing_store: {
        width: canvas.width,
        height: canvas.height,
      },
      logical_arena: state.arena,
    }
  })
}

async function assertPointerMapsToLogicalArena(page: Page): Promise<void> {
  const box = await page.locator('canvas.battle-stage__canvas').boundingBox()
  expect(box).not.toBeNull()
  if (!box) return

  const state = await getGameState(page)
  const canvas = page.locator('canvas.battle-stage__canvas')
  for (const point of [
    { x: 0, y: 0 },
    { x: 1, y: 0 },
    { x: 0, y: 1 },
    { x: 1, y: 1 },
    { x: 0.5, y: 0.5 },
  ]) {
    // Dispatch against the Canvas itself so the right/bottom rectangle edges
    // remain testable; physical hit-testing treats those outer edges as
    // outside the element.
    await canvas.dispatchEvent('pointermove', {
      clientX: box.x + box.width * point.x,
      clientY: box.y + box.height * point.y,
      isPrimary: true,
      pointerId: 1,
    })
    await expect.poll(async () => {
      const current = await getGameState(page)
      return {
        x: Math.round(current.player.aimX),
        y: Math.round(current.player.aimY),
      }
    }).toEqual({
      x: Math.round(state.arena.width * point.x),
      y: Math.round(state.arena.height * point.y),
    })
  }
}

test('assist-player-affordance routes through DOM activation and KeyZ', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.goto('/?playtest_evidence=1')
  await waitForRunningWithCombatActors(page)

  const assistButton = page.locator('[data-action="assist-player"]')
  const assistStatus = page.locator('[data-field="combat-hud-assist-status"]')

  await expect(assistButton).toBeVisible()
  await expect(assistButton).toBeEnabled()
  await expect(assistStatus).toHaveText('Assist ready.')

  // Scope Delta (Issue #1375): the combat HUD's compact new layout
  // (`data-combat-hud`, fewer stacked `.panel` sections than the pre-#1375
  // HUD) sits higher on screen, now underneath the top-right
  // `?playtest_evidence=1` debug panel's covered region at this viewport
  // (`click({ force: true })` still hit-tests at the element's real screen
  // coordinates and would land on that unrelated dev-only overlay instead).
  // This test validates DOM-click -> command-intent routing, not manual
  // pointer hit-testing (covered separately by
  // `tests/e2e/m2-combat-mvp.spec.ts`'s AC5 pointerdown tests), so it
  // invokes the button's own `click()` method directly instead.
  await assistButton.evaluate((button) => (button as HTMLButtonElement).click())
  await expect
    .poll(async () => {
      const state = await getGameState(page)
      return {
        activeIntent: state.commandIntent.activeIntent,
        hasAssignedTarget: state.allies.some((ally) => ally.targetEntityId !== null),
      }
    }, { timeout: 5_000, intervals: [50] })
    .toEqual({
      activeIntent: 'assist_player',
      hasAssignedTarget: true,
    })
  await expect(assistStatus).toHaveText('Allies covering you.')

  await page.keyboard.press('KeyZ')
  await expect
    .poll(async () => {
      const state = await getGameState(page)
      return state.commandIntent.bufferedIntentExpiresAtTick !== null
    }, { timeout: 5_000, intervals: [50] })
    .toBe(true)

  await page.screenshot({
    path: testInfo.outputPath('assist-player-routing.png'),
    fullPage: true,
  })
})

test('assist-player-affordance runtime evidence covers 1280x720, 1366x768, 1920x1080 and 100%, 125%, 150%, 200%', async ({
  page,
}, testInfo) => {
  test.setTimeout(180_000)

  const evidence: EvidenceEntry[] = []

  await page.setViewportSize(SCENARIOS[0].viewport)
  await page.goto('/?playtest_evidence=1')
  await waitForRunningWithCombatActors(page)

  for (const scenario of SCENARIOS) {
    await page.setViewportSize(scenario.viewport)
    await applyBrowserZoom(page, scenario.zoom.factor)
    await page.waitForTimeout(100)

    const assistButton = page.locator('[data-action="assist-player"]')
    const assistStatus = page.locator('[data-field="combat-hud-assist-status"]')

    await expect(assistButton).toBeVisible()
    await expect(assistStatus).toBeVisible()
    await expect(assistButton).toHaveText('Assist allies')
    await expect(assistButton).toHaveAttribute('aria-label', 'Assist allies')
    await expect(assistStatus).toHaveAttribute('role', 'status')
    await expect(assistStatus).toHaveAttribute('aria-live', 'polite')
    await expect(assistStatus).toHaveAttribute('aria-atomic', 'true')

    await assistButton.scrollIntoViewIfNeeded()
    await assistStatus.scrollIntoViewIfNeeded()

    const buttonBox = await assistButton.boundingBox()
    const statusBox = await assistStatus.boundingBox()
    expect(buttonBox).not.toBeNull()
    expect(statusBox).not.toBeNull()
    expect(buttonBox!.x).toBeGreaterThanOrEqual(0)
    expect(buttonBox!.y).toBeGreaterThanOrEqual(0)
    expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(scenario.viewport.width)
    expect(statusBox!.x).toBeGreaterThanOrEqual(0)
    expect(statusBox!.y).toBeGreaterThanOrEqual(0)
    expect(statusBox!.x + statusBox!.width).toBeLessThanOrEqual(scenario.viewport.width)
    expect(buttonBox!.y + buttonBox!.height).toBeLessThanOrEqual(scenario.viewport.height)
    expect(statusBox!.y + statusBox!.height).toBeLessThanOrEqual(scenario.viewport.height)

    await assistButton.focus()
    await expect(assistButton).toBeFocused()
    await expect(assistStatus).toHaveText('Assist ready.')

    const screenshotPath = testInfo.outputPath(
      `assist-player-affordance-${scenario.viewport.label}-${scenario.zoom.label.replace('%', 'pct')}.png`,
    )
    await page.screenshot({
      path: screenshotPath,
      fullPage: true,
    })

    const observed = await collectEvidence(page)
    expect(observed.logical_arena).toEqual({ width: 960, height: 540 })
    expect(observed.canvas_backing_store.width).toBeCloseTo(
      observed.canvas_css.width * observed.observed_devicePixelRatio,
      0,
    )
    expect(observed.canvas_backing_store.height).toBeCloseTo(
      observed.canvas_css.height * observed.observed_devicePixelRatio,
      0,
    )
    evidence.push({
      viewport: scenario.viewport.label,
      browser_zoom: scenario.zoom.label,
      'screenshot path': screenshotPath,
      screenshot_path: screenshotPath,
      ...observed,
    })
  }

  const evidencePath = testInfo.outputPath('assist-player-affordance-evidence.json')
  await writeFile(
    evidencePath,
    JSON.stringify({
      related_issue: '#753',
      overlapping_paths: ['src/render/CanvasRenderer.ts'],
      edit_intent: 'ally marker and assist cue only',
      non_conflict_reason: 'C1 benign overlap; overlay font stack untouched',
      evidence,
    }, null, 2),
    'utf8',
  )
})

test('responsive canvas keeps backing store and pointer mapping aligned at DPR 1, 1.25, and 2', async ({
  browser,
}, testInfo) => {
  test.setTimeout(90_000)
  const evidence: Array<EvidenceEntry & { dpr: number }> = []

  for (const dpr of [1, 1.25, 2]) {
    const context = await browser.newContext({
      viewport: { width: 1280, height: 720 },
      deviceScaleFactor: dpr,
    })
    const page = await context.newPage()
    await page.goto('/')
    await waitForRunningWithCombatActors(page)
    await assertPointerMapsToLogicalArena(page)

    const observed = await collectEvidence(page)
    expect(observed.logical_arena).toEqual({ width: 960, height: 540 })
    expect(observed.canvas_backing_store.width).toBe(Math.round(observed.canvas_css.width * dpr))
    expect(observed.canvas_backing_store.height).toBe(Math.round(observed.canvas_css.height * dpr))

    const screenshotPath = testInfo.outputPath(`responsive-canvas-dpr-${dpr}.png`)
    await page.screenshot({ path: screenshotPath, fullPage: true })
    evidence.push({
      viewport: '1280x720',
      browser_zoom: '100%',
      screenshot_path: screenshotPath,
      dpr,
      ...observed,
    })
    await context.close()
  }

  await writeFile(
    testInfo.outputPath('responsive-canvas-runtime-evidence.json'),
    JSON.stringify({ head_sha: process.env.GITHUB_SHA ?? 'local', evidence }, null, 2),
    'utf8',
  )
})
