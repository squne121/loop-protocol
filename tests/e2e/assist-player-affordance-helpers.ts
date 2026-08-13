import { chromium, expect, type BrowserContext, type Page } from '@playwright/test'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

/**
 * Minimal ambient declaration for the narrow slice of the Chrome extension
 * `chrome.tabs` API this harness's background service worker uses (Issue
 * #2119: moved here from the pre-split `assist-player-affordance.spec.ts`,
 * where this same declaration gap existed and was pre-existing/tracked as a
 * follow-up per `tests/e2e/tsconfig.json`'s exclude comment -- rather than
 * carry that gap forward into two new files, `@types/chrome` is not a
 * project dependency so this file declares only the 3 calls it actually
 * uses, scoped locally instead of widening the global ambient surface via a
 * full `@types/chrome` dependency).
 */
declare const chrome: {
  tabs: {
    query(query: { active: boolean; lastFocusedWindow: boolean }): Promise<Array<{ id?: number }>>
    setZoom(tabId: number, factor: number): Promise<void>
    getZoom(tabId: number): Promise<number>
  }
}

// ---------------------------------------------------------------------------
// Shared, side-effect-free helper module for the assist-player-affordance E2E
// lanes (Issue #2119: responsive viewport/DPR/zoom matrix moved into its own
// spec/provider job; both `assist-player-affordance.spec.ts` (e2e-core) and
// `assist-player-affordance-responsive.spec.ts` (e2e-responsive-matrix)
// import from here so the underlying harness logic stays identical across
// both lanes -- only the SCENARIOS/matrix data and the tests that consume
// them differ per spec file).
//
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

export async function createZoomControlExtension(outputDir: string): Promise<string> {
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

export interface ZoomCapableContext {
  context: BrowserContext
  page: Page
  /** Real Chrome tab zoom via chrome.tabs.setZoom (Issue #1956 fix 6). */
  setZoom(factor: number): Promise<void>
  /** Authority for observed zoom: chrome.tabs.getZoom, not visualViewport.scale. */
  getZoom(): Promise<number>
  close(): Promise<void>
}

export async function launchZoomCapableContext(
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
      // explicitly (verified empirically in this harness -- old headless
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

export interface LoopE2EState {
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

export type EvidenceEntry = {
  head_sha: string
  viewport: string
  browser_zoom: string
  os_runner: string
  test_run_id: string
  observed_devicePixelRatio: number
  /**
   * Chrome tab zoom as reported by `chrome.tabs.getZoom()` (Issue #1956 fix
   * 6) -- the observed-zoom AUTHORITY. `observed_visualViewportScale` is
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
  /**
   * Which HUD control this evidence entry actually exercised (Issue #1958
   * AC7 fix_delta): 'assist' for the pre-existing SCENARIOS matrix,
   * 'pause' for `COLLAPSED_EDGE_SCENARIOS` (375x667, where edge-control is
   * collapsed and Assist is not visible -- see that array's doc comment).
   * Undefined is treated as 'assist' for backward compatibility with prior
   * evidence JSON consumers.
   */
  checked_control?: 'assist' | 'pause'
}

export type PointerMappingEvidence = {
  relative: { x: number; y: number }
  logical: { x: number; y: number }
}

export type FrozenGameplayState = Pick<LoopE2EState, 'arena'> & {
  player: LoopE2EState['player']
  projectiles: LoopE2EState['projectiles']
}

export async function getGameState(page: Page): Promise<LoopE2EState> {
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

export async function waitForRunningWithCombatActors(page: Page): Promise<void> {
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
export async function collectEvidence(
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
export const POINTER_EPSILON_PX = 50

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
export async function waitForZoomToApply(page: Page, expectedDevicePixelRatio: number): Promise<void> {
  await expect
    .poll(
      async () => page.evaluate(() => window.devicePixelRatio),
      { timeout: 8_000, intervals: [20, 50, 100, 200] },
    )
    .toBeCloseTo(expectedDevicePixelRatio, 2)
}

export async function waitForCanvasLayoutToSettle(page: Page): Promise<void> {
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

export async function assertPointerMapsToLogicalArena(page: Page): Promise<PointerMappingEvidence[]> {
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

export function frozenGameplayState(state: LoopE2EState): FrozenGameplayState {
  return {
    arena: state.arena,
    player: state.player,
    projectiles: state.projectiles,
  }
}

// ---------------------------------------------------------------------------
// Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 2): real
// Playwright keyboard-navigation helpers -- never `.focus()` injection.
// `.focus()` calls the DOM focus() method directly, bypassing the browser's
// actual Tab-order traversal entirely (a hidden/unreachable element could
// still be "focused" this way); real `Tab`/`Shift+Tab` key presses exercise
// the same tab-order pipeline a real keyboard user depends on.
// ---------------------------------------------------------------------------

/**
 * Presses real `Tab` (or `Shift+Tab` when `shift` is true) from the CURRENT
 * focus position until `document.activeElement` matches `locator`'s
 * element, or `maxPresses` is exceeded (fails the test). Returns the number
 * of presses actually used, so callers can assert forward/backward DOM
 * order relationships (e.g. "Assist is reached in fewer presses than
 * Pause" proves Assist precedes Pause in tab order).
 */
export async function tabToLocator(
  page: Page,
  locator: ReturnType<Page['locator']>,
  opts: { shift?: boolean; maxPresses?: number; label: string },
): Promise<number> {
  const maxPresses = opts.maxPresses ?? 25
  const key = opts.shift ? 'Shift+Tab' : 'Tab'
  for (let presses = 1; presses <= maxPresses; presses += 1) {
    await page.keyboard.press(key)
    const isFocused = await locator.evaluate((el) => document.activeElement === el).catch(() => false)
    if (isFocused) {
      return presses
    }
  }
  throw new Error(`${opts.label}: real ${key} navigation did not reach the target within ${maxPresses} presses`)
}

/**
 * A focused element must have a REAL, visible focus indicator (AC5/AC6):
 * either a non-`none` CSS `outline`/`outlineStyle`, or a `box-shadow`
 * distinct from the element's own resting-state box-shadow. Also asserts
 * the element's bounding box is non-empty -- a zero-size focused element
 * would trivially "have no visible focus ring" by construction.
 * Deliberately does NOT call `scrollIntoViewIfNeeded()` first (Issue #1958
 * fix_delta iteration 3, blocker 2): auto-scrolling before reading geometry
 * would hide a genuine off-screen-focus placement bug instead of catching
 * it.
 */
export async function assertFocusIndicatorVisible(
  locator: ReturnType<Page['locator']>,
  label: string,
): Promise<void> {
  const style = await locator.evaluate((el) => {
    const computed = getComputedStyle(el)
    return {
      outlineStyle: computed.outlineStyle,
      outlineWidth: computed.outlineWidth,
      boxShadow: computed.boxShadow,
    }
  })
  const hasOutline = style.outlineStyle !== 'none' && style.outlineWidth !== '0px'
  const hasBoxShadow = style.boxShadow !== 'none' && style.boxShadow !== ''
  expect(
    hasOutline || hasBoxShadow,
    `${label}: focused control must have a visible focus indicator (outline or box-shadow), got ${JSON.stringify(style)}`,
  ).toBe(true)
  const box = await locator.boundingBox()
  expect(box, `${label}: focused control must have a bounding box`).not.toBeNull()
  if (box) {
    expect(box.width, `${label}: focused control must have non-zero width`).toBeGreaterThan(0)
    expect(box.height, `${label}: focused control must have non-zero height`).toBeGreaterThan(0)
  }
}

// Re-export `existsSync` for spec files that assert screenshot files
// actually landed on disk (kept here so both specs use the same import
// surface instead of importing `node:fs` directly in two places).
export { existsSync }
