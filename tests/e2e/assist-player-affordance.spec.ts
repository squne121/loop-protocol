import { chromium, expect, test, type BrowserContext, type Page } from '@playwright/test'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

// ---------------------------------------------------------------------------
// Real Chrome tab zoom harness (Issue #1956 fix 6)
//
// `Emulation.setPageScaleFactor` (CDP page/pinch scale) is NOT the same thing
// as an actual Chrome tab zoom (the Ctrl/Cmd +/- equivalent, backed by
// `chrome.tabs.setZoom`/`getZoom`): CDP page scale is a compositor-level
// visual scale that does not itself change `window.devicePixelRatio` and
// does not retrigger a `ResizeObserver` watching
// `{ box: 'device-pixel-content-box' }` (no CSS box size change occurs).
// Real Chrome tab zoom DOES change the effective device pixel ratio and
// DOES retrigger that observer, which is exactly what
// `observeCanvasPresentation` (src/main.ts) depends on to keep the Canvas
// backing store in sync (Issue #1956 fix 3). A minimal Manifest V3
// extension (generated at runtime into Playwright's own test-output
// directory, never committed to the repo) exposes `chrome.tabs.setZoom` /
// `getZoom` from its background service worker; a Playwright persistent
// Chromium context loads it so tests can drive real tab zoom instead of
// CDP page scale.
// ---------------------------------------------------------------------------

async function createZoomControlExtension(outputDir: string): Promise<string> {
  const extDir = path.join(outputDir, 'loop-e2e-zoom-control-extension')
  await mkdir(extDir, { recursive: true })
  await writeFile(
    path.join(extDir, 'manifest.json'),
    JSON.stringify(
      {
        manifest_version: 3,
        name: 'loop-e2e-zoom-control',
        version: '1.0.0',
        description:
          'Test-only harness extension exposing chrome.tabs.setZoom/getZoom to Playwright '
          + '(Issue #1956 fix 6). Generated at runtime under Playwright test-output; never '
          + 'committed and never shipped with the product build.',
        permissions: ['tabs'],
        background: { service_worker: 'background.js' },
      },
      null,
      2,
    ),
    'utf8',
  )
  await writeFile(
    path.join(extDir, 'background.js'),
    // The background service worker is the only extension context with
    // access to chrome.tabs.setZoom/getZoom; the test drives it via
    // Playwright's `serviceWorker.evaluate()`.
    'self.__loopE2EZoomReady = true;',
    'utf8',
  )
  return extDir
}

interface ZoomCapableContext {
  context: BrowserContext
  page: Page
  /** Real Chrome tab zoom via chrome.tabs.setZoom (Issue #1956 fix 6). */
  setZoom(factor: number): Promise<void>
  /** Authority for observed zoom: chrome.tabs.getZoom, not visualViewport.scale. */
  getZoom(): Promise<number>
  close(): Promise<void>
}

async function launchZoomCapableContext(
  outputDir: string,
  options: { viewport: { width: number; height: number }; deviceScaleFactor?: number },
): Promise<ZoomCapableContext> {
  const extDir = await createZoomControlExtension(outputDir)
  const userDataDir = await mkdtemp(path.join(os.tmpdir(), 'loop-e2e-zoom-'))

  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    viewport: options.viewport,
    deviceScaleFactor: options.deviceScaleFactor,
    args: [
      // Playwright's default headless launch predates Chromium's
      // extension-capable "new" headless mode; extensions require it
      // explicitly (verified empirically in this harness — old headless
      // mode times out waiting for the extension's service worker).
      '--headless=new',
      `--disable-extensions-except=${extDir}`,
      `--load-extension=${extDir}`,
      '--no-sandbox',
    ],
  })

  let serviceWorker = context.serviceWorkers()[0]
  if (!serviceWorker) {
    serviceWorker = await context.waitForEvent('serviceworker', { timeout: 15_000 })
  }

  const page = context.pages()[0] ?? (await context.newPage())

  async function resolveActiveTabId(): Promise<number> {
    return serviceWorker.evaluate(async () => {
      const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
      const tab = tabs[0]
      if (!tab || tab.id === undefined) {
        throw new Error('loop-e2e-zoom-control: no active tab found')
      }
      return tab.id
    })
  }

  return {
    context,
    page,
    async setZoom(factor: number) {
      const tabId = await resolveActiveTabId()
      await serviceWorker.evaluate(
        async ({ tabId, factor }) => {
          await chrome.tabs.setZoom(tabId, factor)
        },
        { tabId, factor },
      )
    },
    async getZoom() {
      const tabId = await resolveActiveTabId()
      return serviceWorker.evaluate((tabId) => chrome.tabs.getZoom(tabId), tabId)
    },
    async close() {
      await context.close()
    },
  }
}

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
    x: number
    y: number
    aimX: number
    aimY: number
  }
  projectiles: Array<{
    id: number
    x: number
    y: number
  }>
  input: {
    pointerX: number
    pointerY: number
    pointerKnown: boolean
  }
}

type Scenario = {
  viewport: { width: number; height: number; label: string }
  zoom: { factor: number; label: string }
}

type EvidenceEntry = {
  head_sha: string
  viewport: string
  browser_zoom: string
  os_runner: string
  test_run_id: string
  observed_devicePixelRatio: number
  /**
   * Chrome tab zoom as reported by `chrome.tabs.getZoom()` (Issue #1956 fix
   * 6) — the observed-zoom AUTHORITY. `observed_visualViewportScale` is
   * kept only as supplementary telemetry, never as the authority.
   */
  observed_chrome_tab_zoom: number
  observed_visualViewportScale: number | null
  userAgent: string
  screenshot_path: string
  canvas_css: { width: number; height: number }
  canvas_backing_store: { width: number; height: number }
  logical_arena: { width: number; height: number }
  pointer_mapping?: PointerMappingEvidence[]
  frozen_gameplay?: FrozenGameplayState
}

type PointerMappingEvidence = {
  relative: { x: number; y: number }
  logical: { x: number; y: number }
}

type FrozenGameplayState = Pick<LoopE2EState, 'arena'> & {
  player: LoopE2EState['player']
  projectiles: LoopE2EState['projectiles']
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

const RESPONSIVE_VIEWPORTS = [
  { width: 1280, height: 720, label: '1280x720' },
  { width: 1366, height: 768, label: '1366x768' },
  { width: 1920, height: 1080, label: '1920x1080' },
  { width: 1437, height: 1365, label: '1437x1365' },
]

const RESPONSIVE_DPRS = [1, 1.25, 2, 0.667]
const RESPONSIVE_ZOOMS = [
  { factor: 1, label: '100%' },
  { factor: 1.25, label: '125%' },
  { factor: 1.5, label: '150%' },
  { factor: 2, label: '200%' },
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

/**
 * Page-observable presentation metrics only (Issue #1956 fix 6). Chrome tab
 * zoom itself (`chrome.tabs.getZoom()`) is NOT observable from page context
 * -- only from the extension's background service worker -- so callers
 * fetch it separately via `ZoomCapableContext.getZoom()` and merge it into
 * the evidence entry as `observed_chrome_tab_zoom`.
 */
async function collectEvidence(
  page: Page,
): Promise<
  Omit<
    EvidenceEntry,
    'head_sha' | 'viewport' | 'browser_zoom' | 'screenshot_path' | 'os_runner' | 'test_run_id' | 'observed_chrome_tab_zoom'
  >
> {
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

async function resolveHeadSha(): Promise<string> {
  if (process.env.GITHUB_SHA) {
    return process.env.GITHUB_SHA
  }
  try {
    const { execSync } = await import('node:child_process')
    return execSync('git rev-parse HEAD', { encoding: 'utf8' }).trim()
  } catch {
    return 'unknown-head-sha'
  }
}

/**
 * Relative fractional offset for the "epsilon-inside" corner points below
 * (Issue #1956 fix 6): a real OS-level `page.mouse.move()` cannot physically
 * land on an exact 0%/100% boundary pixel the way a synthetic
 * `dispatchEvent('pointermove', ...)` could -- that exact-boundary math
 * remains a unit-test-only concern (`tests/input-bindings.test.ts`'s
 * "CSS-scaled canvas" corner test). This E2E instead exercises real input a
 * few CSS pixels inside each edge.
 *
 * Must clear `.battle-stage__viewport`'s own 28px bottom corner
 * `border-radius` (src/style.css): a real pointer physically CANNOT hit the
 * rounded-off corner cutout the way a synthetic `dispatchEvent` could --
 * `document.elementFromPoint()` correctly resolves a point inside that
 * quarter-circle cutout to whatever is behind the clipped element (verified
 * empirically: a 4px epsilon reliably lands in the cutout on both bottom
 * corners and never reaches the Canvas at all). 34px clears the 28px radius
 * on both axes.
 */
const POINTER_EPSILON_PX = 50

/**
 * Wait for the real Chrome tab zoom (Issue #1956 fix 6) to have actually
 * propagated to the page's rendering pipeline before trusting any
 * `getBoundingClientRect()` read.
 *
 * Root cause (Issue #1956 responsive-canvas iteration 2 fix): a prior
 * version of this harness called `zoomCtx.setZoom(zoom.factor)` and then
 * immediately proceeded to poll `getBoundingClientRect()` for two
 * consecutive stable reads via `waitForCanvasLayoutToSettle()`. That
 * `await` on `chrome.tabs.setZoom()` (a background-service-worker-side
 * extension API call) resolves once the BROWSER PROCESS has accepted the
 * new zoom level -- it does NOT wait for that zoom change to have actually
 * propagated to the PAGE's renderer process and retriggered layout. Two
 * consecutive `getBoundingClientRect()` reads taken before that
 * propagation lands are trivially "stable" (both still report the OLD,
 * pre-zoom geometry), so `waitForCanvasLayoutToSettle()` falsely declared
 * settlement on stale geometry. This was empirically confirmed with debug
 * instrumentation: `window.devicePixelRatio` itself (the earliest
 * page-observable signal that a real Chrome tab zoom has taken visual
 * effect -- see the module header comment) was ALSO still reporting the
 * previous zoom's value at that point, not merely the CSS box.
 *
 * `window.devicePixelRatio` for a real Chrome tab zoom is the context's
 * declared `deviceScaleFactor` multiplied by the current chrome tab zoom
 * factor (see module header comment). Polling for THIS specific target
 * value (rather than mere read-to-read stability) closes the race: it is
 * the earliest reliable page-observable proof that the zoom has actually
 * been applied to this page's rendering, so any `getBoundingClientRect()`
 * read taken afterward is guaranteed to reflect the new, settled geometry.
 */
async function waitForZoomToApply(page: Page, expectedDevicePixelRatio: number): Promise<void> {
  await expect
    .poll(
      async () => page.evaluate(() => window.devicePixelRatio),
      { timeout: 8_000, intervals: [20, 50, 100, 200] },
    )
    .toBeCloseTo(expectedDevicePixelRatio, 2)
}

async function waitForCanvasLayoutToSettle(page: Page): Promise<void> {
  // Empirically observed in this harness: after setViewportSize()/real
  // Chrome tab zoom, the Canvas's own CSS layout box can keep changing
  // (growing/shrinking) for a period well beyond the backing-store settle
  // poll above -- a stale `boundingBox()` snapshot taken mid-transition
  // produces pointer targets that no longer correspond to the Canvas's
  // final geometry. Poll `getBoundingClientRect()` itself until it reports
  // the same box on two consecutive checks.
  let previous: { width: number; height: number; x: number; y: number } | null = null
  await expect
    .poll(
      async () => {
        const rect = await page.evaluate(() => {
          const el = document.querySelector('canvas.battle-stage__canvas')
          if (!el) return null
          const r = el.getBoundingClientRect()
          return { width: r.width, height: r.height, x: r.x, y: r.y }
        })
        const stable = previous !== null && rect !== null
          && Math.abs(rect.width - previous.width) < 0.5
          && Math.abs(rect.height - previous.height) < 0.5
          && Math.abs(rect.x - previous.x) < 0.5
          && Math.abs(rect.y - previous.y) < 0.5
        previous = rect
        return stable
      },
      { timeout: 8_000, intervals: [100, 150, 250, 400] },
    )
    .toBe(true)
}

async function assertPointerMapsToLogicalArena(page: Page): Promise<PointerMappingEvidence[]> {
  await waitForCanvasLayoutToSettle(page)
  const initialBox = await page.locator('canvas.battle-stage__canvas').boundingBox()
  expect(initialBox).not.toBeNull()
  if (!initialBox) return []

  const state = await getGameState(page)
  const evidence: PointerMappingEvidence[] = []
  const epsilonFracX = POINTER_EPSILON_PX / initialBox.width
  const epsilonFracY = POINTER_EPSILON_PX / initialBox.height
  for (const point of [
    { x: epsilonFracX, y: epsilonFracY }, // top-left, epsilon-inside
    { x: 1 - epsilonFracX, y: epsilonFracY }, // top-right, epsilon-inside
    { x: epsilonFracX, y: 1 - epsilonFracY }, // bottom-left, epsilon-inside
    { x: 1 - epsilonFracX, y: 1 - epsilonFracY }, // bottom-right, epsilon-inside
    { x: 0.5, y: 0.5 }, // center
  ]) {
    // A real OS-level pointer can only hit a point that is actually inside
    // the current browser viewport (Issue #1956 fix 6): when the Canvas is
    // taller than the viewport (e.g. a tall narrow RESPONSIVE_VIEWPORTS
    // entry), the target y fraction may fall below `window.innerHeight`
    // until the page scrolls. Scroll first so the target point's document
    // position is centered in the viewport, THEN re-read the Canvas's
    // now-viewport-relative bounding box before moving the mouse -- a
    // synthetic dispatchEvent never needed this because it bypassed real
    // hit-testing/scroll entirely.
    await page.evaluate(
      ({ fracY }) => {
        const el = document.querySelector('canvas.battle-stage__canvas')
        if (!el) return
        const rect = el.getBoundingClientRect()
        const documentTop = rect.top + window.scrollY
        const targetDocumentY = documentTop + rect.height * fracY
        const desiredScrollY = targetDocumentY - window.innerHeight / 2
        window.scrollTo(0, Math.max(0, desiredScrollY))
      },
      { fracY: point.y },
    )

    const box = await page.locator('canvas.battle-stage__canvas').boundingBox()
    expect(box).not.toBeNull()
    if (!box) continue

    // Real OS-level pointer input (Issue #1956 fix 6), not a synthetic
    // dispatchEvent -- this exercises the actual browser hit-testing and
    // pointermove pipeline the way a real player interacts with the Canvas.
    // The combat HUD (`[data-combat-hud]`) is an intentionally interactive
    // overlay anchored top-right of the Canvas (`.battle-hud-layer` --
    // `justify-items: end`); at a small enough Canvas it can legitimately
    // extend far enough down to cover this test's own right-side
    // epsilon-inside corner points. That is real, correct app behavior (a
    // real click there WOULD hit the HUD, not the Canvas) -- nudge the
    // real-pointer target left of the HUD's bounding box (not further "in"
    // toward the corner) rather than asserting through it.
    let targetX = box.x + box.width * point.x
    const targetY = box.y + box.height * point.y
    const hudBox = await page.locator('[data-combat-hud]').boundingBox().catch(() => null)
    if (hudBox && targetX >= hudBox.x && targetX <= hudBox.x + hudBox.width
      && targetY >= hudBox.y && targetY <= hudBox.y + hudBox.height) {
      targetX = Math.max(box.x, hudBox.x - POINTER_EPSILON_PX)
    }
    await page.mouse.move(targetX, targetY)
    // Recompute the actual hit fraction from the (possibly HUD-adjusted)
    // targetX/targetY -- the expected logical coordinate must reflect where
    // the pointer really landed, not the original unadjusted corner
    // fraction.
    const actualFracX = (targetX - box.x) / box.width
    const actualFracY = (targetY - box.y) / box.height
    const expected = {
      x: Math.round(state.arena.width * actualFracX),
      y: Math.round(state.arena.height * actualFracY),
      known: true,
    }
    // A single real page.mouse.move() event occasionally does not land
    // within the default poll window in this harness (observed empirically:
    // rare single-attempt event-delivery lag right after a viewport/zoom
    // change) -- retry the move itself, not just the poll. Moving to the
    // SAME (x, y) on every retry is not sufficient: if the OS-level cursor
    // is already at that exact position, a repeated `page.mouse.move()` to
    // the identical coordinates does not necessarily redeliver a
    // `pointermove` event, so retries alone would never self-heal from a
    // dropped first event. Nudge the cursor slightly away and back on each
    // retry attempt to guarantee an actual position change (and therefore a
    // real pointermove) every time.
    await expect(async () => {
      // Always nudge away first: guarantees a genuine position CHANGE on
      // every attempt (including the first), so the browser is guaranteed
      // to dispatch a real pointermove rather than silently deduplicating a
      // move to a coordinate the cursor may already be resting at.
      const nudgeX = Math.max(box.x, Math.min(box.x + box.width, targetX + 2))
      const nudgeY = Math.max(box.y, Math.min(box.y + box.height, targetY + 2))
      await page.mouse.move(nudgeX, nudgeY, { steps: 2 })
      await page.mouse.move(targetX, targetY, { steps: 2 })
      const current = await getGameState(page)
      expect({
        x: Math.round(current.input.pointerX),
        y: Math.round(current.input.pointerY),
        known: current.input.pointerKnown,
      }).toEqual(expected)
    }).toPass({ timeout: 10_000, intervals: [50, 100, 250, 500] })
    evidence.push({
      relative: { x: actualFracX, y: actualFracY },
      logical: { x: expected.x, y: expected.y },
    })
  }
  return evidence
}

function frozenGameplayState(state: LoopE2EState): FrozenGameplayState {
  return {
    arena: state.arena,
    player: state.player,
    projectiles: state.projectiles,
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

// eslint-disable-next-line no-empty-pattern -- Playwright requires the first arg to be a (possibly empty) fixtures destructure.
test('assist-player-affordance runtime evidence covers 1280x720, 1366x768, 1920x1080 and 100%, 125%, 150%, 200%', async ({}, testInfo) => {
  test.setTimeout(240_000)

  const evidence: EvidenceEntry[] = []
  const headSha = await resolveHeadSha()
  const osRunner = `${os.platform()} ${os.release()} ${os.arch()}`

  // Real Chrome tab zoom harness (Issue #1956 fix 6): one persistent
  // extension-loaded context per distinct viewport in SCENARIOS, so
  // `chrome.tabs.setZoom` drives the actual browser zoom for every cell
  // instead of CDP page-scale emulation.
  const viewportGroups = new Map<string, typeof SCENARIOS>()
  for (const scenario of SCENARIOS) {
    const key = scenario.viewport.label
    const group = viewportGroups.get(key) ?? []
    group.push(scenario)
    viewportGroups.set(key, group)
  }

  for (const [, scenarios] of viewportGroups) {
    const zoomCtx = await launchZoomCapableContext(testInfo.outputDir, {
      viewport: scenarios[0].viewport,
    })
    try {
      const page = zoomCtx.page
      await page.goto('/?playtest_evidence=1')
      await waitForRunningWithCombatActors(page)

      for (const scenario of scenarios) {
        await zoomCtx.setZoom(scenario.zoom.factor)
        await page.waitForTimeout(150)

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
        // Fix 6 point 4: every matrix cell must have a REAL screenshot file
        // on disk -- no 'not-captured' placeholder path.
        expect(existsSync(screenshotPath), `screenshot must exist on disk: ${screenshotPath}`).toBe(true)

        const observedZoom = await zoomCtx.getZoom()
        const observed = await collectEvidence(page)
        expect(observed.logical_arena).toEqual({ width: 960, height: 540 })
        // A real Chrome tab zoom (Issue #1956 fix 6) introduces genuine
        // sub-pixel rounding between the ResizeObserver-reported device
        // pixel box and an independently-read CSS rect at evidence-collection
        // time (unlike CDP page-scale's exact synthetic math) -- allow up to
        // 1 device pixel of legitimate rounding drift, not the stricter 0.5px
        // `toBeCloseTo(x, 0)` tolerance.
        expect(
          Math.abs(
            observed.canvas_backing_store.width - observed.canvas_css.width * observed.observed_devicePixelRatio,
          ),
        ).toBeLessThanOrEqual(1)
        expect(
          Math.abs(
            observed.canvas_backing_store.height - observed.canvas_css.height * observed.observed_devicePixelRatio,
          ),
        ).toBeLessThanOrEqual(1)
        evidence.push({
          head_sha: headSha,
          viewport: scenario.viewport.label,
          browser_zoom: scenario.zoom.label,
          os_runner: osRunner,
          test_run_id: testInfo.testId,
          observed_chrome_tab_zoom: observedZoom,
          screenshot_path: screenshotPath,
          ...observed,
        })
      }
    } finally {
      await zoomCtx.close()
    }
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

// eslint-disable-next-line no-empty-pattern -- Playwright requires the first arg to be a (possibly empty) fixtures destructure.
test('responsive canvas preserves the logical arena, frozen combat positions, backing store, and pointer mapping across viewport, DPR, and zoom', async ({}, testInfo) => {
  test.setTimeout(300_000)
  const evidence: Array<EvidenceEntry & { declared_dpr: number }> = []
  const headSha = await resolveHeadSha()
  const osRunner = `${os.platform()} ${os.release()} ${os.arch()}`

  for (const dpr of RESPONSIVE_DPRS) {
    // Real Chrome tab zoom harness (Issue #1956 fix 6): deviceScaleFactor is
    // fixed for the lifetime of a persistent context, so one zoom-capable
    // context is launched per declared DPR; zoom itself is driven live via
    // chrome.tabs.setZoom within that context for every viewport x zoom cell.
    const zoomCtx = await launchZoomCapableContext(testInfo.outputDir, {
      viewport: RESPONSIVE_VIEWPORTS[0],
      deviceScaleFactor: dpr,
    })
    try {
      const page = zoomCtx.page
      await page.goto('/')
      await waitForRunningWithCombatActors(page)

      const canvas = page.locator('canvas.battle-stage__canvas')
      await canvas.hover({ position: { x: 480, y: 270 } })
      await page.mouse.down()
      await expect.poll(async () => (await getGameState(page)).projectiles.length).toBeGreaterThan(0)
      await page.mouse.up()
      await page.locator('[data-action="toggle-pause"]').evaluate((button) => (button as HTMLButtonElement).click())
      await expect(page.locator('[data-action="toggle-pause"]')).toHaveAttribute('aria-pressed', 'true')

      const frozenBeforeResize = frozenGameplayState(await getGameState(page))
      expect(frozenBeforeResize.arena).toEqual({ width: 960, height: 540 })

      for (const viewport of RESPONSIVE_VIEWPORTS) {
        for (const zoom of RESPONSIVE_ZOOMS) {
          await page.setViewportSize(viewport)
          await zoomCtx.setZoom(zoom.factor)
          // Issue #1956 responsive-canvas iteration 2 fix: `setZoom()`
          // resolving does not guarantee the zoom has propagated to this
          // page's renderer yet (see `waitForZoomToApply()` doc comment) --
          // wait for the page-observable `devicePixelRatio` to actually
          // reach the expected (declared DPR x zoom factor) value before
          // trusting any subsequent `getBoundingClientRect()` read.
          await waitForZoomToApply(page, dpr * zoom.factor)
          // Reset the (virtual) cursor to a known-good position inside the
          // new viewport immediately after every resize/zoom change --
          // otherwise it may still be resting at a coordinate from the
          // previous (larger) viewport that now falls outside the new one,
          // which was observed empirically to desynchronize subsequent
          // page.mouse.move() position tracking across rapid successive
          // viewport/zoom changes in this harness.
          await page.mouse.move(10, 10)

          // The ResizeObserver-driven presentation update (Issue #1956 fix
          // 3) is asynchronous relative to setViewportSize()/setZoom() --
          // poll until the backing store has actually settled to the new
          // CSS size x DPR instead of a fixed sleep (which was empirically
          // flaky: 150ms was not always enough for the observer callback to
          // fire and CanvasRenderer.resize() to apply before evidence was
          // collected).
          await expect
            .poll(
              async () => {
                const snapshot = await collectEvidence(page)
                return Math.abs(
                  snapshot.canvas_backing_store.width
                    - Math.round(snapshot.canvas_css.width * snapshot.observed_devicePixelRatio),
                )
              },
              { timeout: 5_000, intervals: [50, 100, 250] },
            )
            .toBeLessThanOrEqual(1)

          const pointerMapping = await assertPointerMapsToLogicalArena(page)
          const observed = await collectEvidence(page)
          const observedZoom = await zoomCtx.getZoom()
          const frozenAfterResize = frozenGameplayState(await getGameState(page))

          expect(observed.logical_arena).toEqual({ width: 960, height: 540 })
          expect(observed.canvas_css.width).toBeGreaterThan(0)
          expect(observed.canvas_css.height).toBeGreaterThan(0)
          expect(observed.canvas_css.height).toBeCloseTo(observed.canvas_css.width * 9 / 16, 2)
          // A real Chrome tab zoom (Issue #1956 fix 6) introduces genuine
          // sub-pixel rounding between the ResizeObserver-reported device
          // pixel box and an independently-read CSS rect at evidence-
          // collection time -- allow up to 1 device pixel of legitimate
          // rounding drift (see the equivalent tolerance in the scenario
          // matrix test above).
          expect(
            Math.abs(
              observed.canvas_backing_store.width
                - Math.round(observed.canvas_css.width * observed.observed_devicePixelRatio),
            ),
          ).toBeLessThanOrEqual(1)
          expect(
            Math.abs(
              observed.canvas_backing_store.height
                - Math.round(observed.canvas_css.height * observed.observed_devicePixelRatio),
            ),
          ).toBeLessThanOrEqual(1)
          expect(observedZoom).toBeCloseTo(zoom.factor, 2)
          expect(frozenAfterResize).toEqual(frozenBeforeResize)

          // Fix 6 point 4: every matrix cell gets a real screenshot; no
          // 'not-captured' placeholder path.
          const screenshotPath = testInfo.outputPath(
            `responsive-canvas-dpr${dpr}-${viewport.label}-${zoom.label.replace('%', 'pct')}.png`,
          )
          await page.screenshot({ path: screenshotPath })
          expect(existsSync(screenshotPath), `screenshot must exist on disk: ${screenshotPath}`).toBe(true)

          evidence.push({
            head_sha: headSha,
            viewport: viewport.label,
            browser_zoom: zoom.label,
            os_runner: osRunner,
            test_run_id: testInfo.testId,
            observed_chrome_tab_zoom: observedZoom,
            screenshot_path: screenshotPath,
            declared_dpr: dpr,
            pointer_mapping: pointerMapping,
            frozen_gameplay: frozenAfterResize,
            ...observed,
          })
        }
      }
    } finally {
      await zoomCtx.close()
    }
  }

  await writeFile(
    testInfo.outputPath('responsive-canvas-runtime-evidence.json'),
    JSON.stringify({
      head_sha: headSha,
      matrix: {
        viewports: RESPONSIVE_VIEWPORTS,
        device_scale_factors: RESPONSIVE_DPRS,
        browser_zooms: RESPONSIVE_ZOOMS,
      },
      evidence,
    }, null, 2),
    'utf8',
  )
})
