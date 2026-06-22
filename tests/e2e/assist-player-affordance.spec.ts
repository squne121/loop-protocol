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
    }
  })
}

test('assist-player-affordance routes through DOM activation and KeyZ', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.goto('/?playtest_evidence=1')
  await waitForRunningWithCombatActors(page)

  const assistButton = page.locator('[data-action="assist-player"]')
  const assistStatus = page.locator('[data-field="assist-status"]')

  await expect(assistButton).toBeVisible()
  await expect(assistButton).toBeEnabled()
  await expect(assistStatus).toHaveText('Assist ready.')

  await assistButton.click()
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
    const assistStatus = page.locator('[data-field="assist-status"]')

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
