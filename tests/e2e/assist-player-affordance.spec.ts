import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import os from 'node:os'
import { resolveExpectedHeadSha } from './visual-utils'
import {
  assertFocusIndicatorVisible,
  collectEvidence,
  existsSync,
  getGameState,
  launchZoomCapableContext,
  tabToLocator,
  waitForRunningWithCombatActors,
  type EvidenceEntry,
  type LoopE2EState,
} from './assist-player-affordance-helpers'

// ---------------------------------------------------------------------------
// Issue #2119: the responsive viewport/DPR/zoom matrix ("responsive canvas
// preserves the logical arena, frozen combat positions, backing store, and
// pointer mapping across viewport, DPR, and zoom") was the single largest
// hotspot in this spec (~3.2min of a ~6.5min standard E2E run) and has been
// physically moved to `assist-player-affordance-responsive.spec.ts`, which
// runs exclusively in the `e2e-responsive-matrix` CI provider job/lane
// (playwright.config.ts `LOOP_E2E_LANE=responsive`). This spec (the
// `e2e-core` lane) intentionally does NOT import or run that test — see
// `docs/dev/test-lane-policy.md` for the exclusive-lane contract. Shared,
// side-effect-free harness helpers used by both specs live in
// `assist-player-affordance-helpers.ts`.
// ---------------------------------------------------------------------------

type Scenario = {
  viewport: { width: number; height: number; label: string }
  zoom: { factor: number; label: string }
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
  // Issue #1958 AC7 (PR #2006 review fix_delta iteration 1, blocker 2): the
  // owner-playtest-reported desktop DPR-approximation viewport, added
  // alongside the pre-existing three. Edge-control (Weapon/Assist) is NOT
  // collapsed at this width (`src/style.css`'s `@media (max-width: 420px)`
  // only fires below 420px), so this reuses the same Assist-button-based
  // evidence flow as the other three viewports.
  { viewport: { width: 1437, height: 1365, label: '1437x1365' }, zoom: { factor: 1, label: '100%' } },
  { viewport: { width: 1437, height: 1365, label: '1437x1365' }, zoom: { factor: 1.25, label: '125%' } },
  { viewport: { width: 1437, height: 1365, label: '1437x1365' }, zoom: { factor: 1.5, label: '150%' } },
  { viewport: { width: 1437, height: 1365, label: '1437x1365' }, zoom: { factor: 2, label: '200%' } },
]

// Issue #1958 AC7 (PR #2006 review fix_delta iteration 1, blocker 2): the
// `Supported minimum viewport` (AC2) 375x667. Unlike the `SCENARIOS` above,
// edge-control (Weapon/Assist) is intentionally collapsed/hidden at this
// width by `src/style.css`'s `@media (max-width: 420px)` progressive-
// disclosure rule (AC1/AC2 semantic state table) -- the Assist button is
// not visible here, so an Assist-based evidence flow would fail by design,
// not by regression. This scenario matrix instead exercises the
// never-collapsing Pause control (AC1: `collapsible: false`), which stays
// reachable at every supported viewport including the minimum.
const COLLAPSED_EDGE_SCENARIOS: Scenario[] = [
  { viewport: { width: 375, height: 667, label: '375x667' }, zoom: { factor: 1, label: '100%' } },
  { viewport: { width: 375, height: 667, label: '375x667' }, zoom: { factor: 1.25, label: '125%' } },
  { viewport: { width: 375, height: 667, label: '375x667' }, zoom: { factor: 1.5, label: '150%' } },
  { viewport: { width: 375, height: 667, label: '375x667' }, zoom: { factor: 2, label: '200%' } },
]

test('assist-player-affordance routes through a real pointer click and KeyZ', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 2): this
  // interaction test no longer opts into the `?playtest_evidence=1` debug
  // panel (`src/ui/playtestEvidence.ts`) -- that panel is entirely opt-in
  // and this test never reads it, so navigating without it removes the
  // panel's real screen-space overlap with the combat HUD instead of
  // routing around the overlap with a programmatic `.evaluate(...click())`
  // bypass. `getByRole(...).click()` below is a genuine Playwright
  // actionability-checked pointer click (visible/stable/enabled/receives-
  // events), the same pipeline `tests/e2e/m2-combat-mvp.spec.ts`'s AC5
  // pointerdown tests exercise for direct Canvas hit-testing.
  await page.goto('/')
  await waitForRunningWithCombatActors(page)

  const assistButton = page.getByRole('button', { name: 'Assist allies' })
  const assistStatus = page.locator('[data-field="combat-hud-assist-status"]')

  await expect(assistButton).toBeVisible()
  await expect(assistButton).toBeEnabled()
  await expect(assistStatus).toHaveText('Assist ready.')

  // Real pointer click (Issue #1958 AC5): genuine actionability-checked
  // click, not a programmatic `element.click()` DOM method call.
  //
  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 2)
  // follow-up finding: `commandIntentRuntime.activeIntent` is a
  // DELIBERATELY short-lived, one-shot transient (`assistPlayerTtlTicks`,
  // `src/state/GameState.ts` -- 133ms TTL by production default) --
  // production/gameplay behavior, out of this Issue's Allowed Paths and not
  // something this test changes. A real Playwright `.click()`'s
  // actionability wait (visible/stable/receives-events) pushes the actual
  // event dispatch later than a synthetic same-microtask `.evaluate(...
  // click())` call, and the observable `assist_player` window empirically
  // opens ~10-40ms AFTER `.click()` resolves and lasts only ~20-30ms
  // (measured via a 5ms-interval sampling probe against this exact spec
  // during investigation) before the buffered intent's TTL lapses and
  // `activeIntent` permanently reverts to `'none'` -- it is a genuine
  // one-shot transition, not a value that can be re-observed later. A
  // `expect.poll` with a 50ms interval can systematically straddle and
  // miss a ~30ms window that opens at +10ms to +40ms (poll samples land at
  // ~0ms and ~50ms, both outside the window) -- this is exactly what was
  // observed empirically (100% reproducible miss with a 50ms interval,
  // reliably caught with a 10ms interval). A short, dense poll interval is
  // therefore required to reliably OBSERVE this real (not fabricated)
  // transient state, not a retry-driven `toPass()` (repeated re-clicks
  // would each restart their own one-shot window and do not help close a
  // single-click observation gap).
  await assistButton.click()
  // Reads command-intent state AND the rendered status copy in the SAME
  // `page.evaluate()` round trip (an atomic snapshot of both), rather than
  // two separate assertions -- the DOM text is a pure render of the same
  // transient state, so checking it via a second, separately-timed
  // assertion after the state poll succeeds would reopen the exact same
  // race window this fix addresses.
  await expect
    .poll(async () => {
      const snapshot = await page.evaluate(() => {
        const hook = (
          window as Window & {
            __LOOP_E2E__?: { getState: () => LoopE2EState }
          }
        ).__LOOP_E2E__
        if (!hook) {
          throw new Error('__LOOP_E2E__ hook not found. Was the app built with VITE_E2E_MODE=true?')
        }
        const state = hook.getState()
        const statusEl = document.querySelector('[data-field="combat-hud-assist-status"]')
        return {
          activeIntent: state.commandIntent.activeIntent,
          hasAssignedTarget: state.allies.some((ally) => ally.targetEntityId !== null),
          statusText: statusEl?.textContent ?? null,
        }
      })
      return snapshot
    }, { timeout: 2_000, intervals: [10] })
    .toEqual({
      activeIntent: 'assist_player',
      hasAssignedTarget: true,
      statusText: 'Allies covering you.',
    })

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

test('Assist and Pause are reachable via real Tab/Shift+Tab order, activate on Enter and Space, and keep a visible focus indicator', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.goto('/')
  await waitForRunningWithCombatActors(page)

  const assistButton = page.getByRole('button', { name: 'Assist allies' })
    const pauseButton = page.getByRole('button', { name: 'Pause' })
    const resumeButton = page.locator('[data-action="resume"]')
    const assistStatus = page.locator('[data-field="combat-hud-assist-status"]')

    // Start from a known, non-focused baseline (a real pointer click on an
    // empty area, not a synthetic focus() call).
    await page.locator('body').click({ position: { x: 1, y: 1 } })

    // Real forward Tab order: Assist must be reached before Pause (matches
    // the DOM order in `COMBAT_HUD_MARKUP`, `src/ui/combatHud.ts`: the
    // Assist button precedes the Pause button).
    const assistPresses = await tabToLocator(page, assistButton, { label: 'Assist allies (forward Tab)' })
    expect(assistPresses, 'Assist allies must be reachable via real forward Tab navigation').toBeGreaterThan(0)
    await assertFocusIndicatorVisible(assistButton, 'Assist allies (forward Tab)')

    const pausePresses = await tabToLocator(page, pauseButton, { label: 'Pause (forward Tab, continuing from Assist)' })
    expect(
      pausePresses,
      'Pause must be reached strictly after Assist in forward Tab order (DOM order)',
    ).toBeGreaterThan(0)
    await assertFocusIndicatorVisible(pauseButton, 'Pause (forward Tab)')

    // Real backward Shift+Tab order: from Pause, Shift+Tab must reach
    // Assist again (reverse of the forward order just proven above).
    const assistBackwardPresses = await tabToLocator(page, assistButton, {
      shift: true,
      label: 'Assist allies (backward Shift+Tab from Pause)',
    })
    expect(assistBackwardPresses).toBeGreaterThan(0)
    await assertFocusIndicatorVisible(assistButton, 'Assist allies (backward Shift+Tab)')

    // Enter activates Assist (real keyboard activation on the focused
    // native <button>, not a programmatic click()).
    await expect(assistButton).toBeFocused()
    await page.keyboard.press('Enter')
    await expect
      .poll(async () => (await getGameState(page)).commandIntent.activeIntent, { timeout: 5_000, intervals: [50] })
      .toBe('assist_player')
    await expect(assistStatus).toHaveText('Allies covering you.')

    // Space activates Pause (real keyboard activation) -- exercised
    // separately from Enter (AC5 requires both).
    await tabToLocator(page, pauseButton, { label: 'Pause (forward Tab, for Space activation)' })
    await expect(pauseButton).toBeFocused()
    await page.keyboard.press('Space')
    await expect(resumeButton).toBeVisible()
    // Pausing makes the combat HUD `inert` (Issue #1376 AC4: it drops out of
    // the accessibility tree/tab order behind the pause dialog), so an
    // accessible-name-based `getByRole` query on the (now inert) Pause
    // button spuriously reports "not found" here -- the same reason
    // `tests/e2e/hud-hull-overflow.spec.ts`'s 375x667 dual-fixture test uses
    // the `[data-action]` CSS selector instead of `getByRole` after
    // pausing.
    await expect(page.locator('[data-action="toggle-pause"]')).toHaveAttribute('aria-pressed', 'true')
    await page.keyboard.press('Escape')
    await expect(resumeButton).toBeHidden()

  await page.screenshot({
    path: testInfo.outputPath('assist-pause-keyboard-activation.png'),
    fullPage: true,
  })
})

// eslint-disable-next-line no-empty-pattern -- Playwright requires the first arg to be a (possibly empty) fixtures destructure.
test('assist-player-affordance runtime evidence covers 1280x720, 1366x768, 1920x1080, 1437x1365, 375x667 and 100%, 125%, 150%, 200%', async ({}, testInfo) => {
  test.setTimeout(240_000)

  const evidence: EvidenceEntry[] = []
  const headSha = resolveExpectedHeadSha()
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

        // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 2):
        // deliberately no `scrollIntoViewIfNeeded()` before reading
        // geometry below -- auto-scrolling first would make the
        // "bounding box is within the viewport" assertions below
        // near-vacuous (a genuine off-screen placement bug would be
        // silently scrolled away instead of caught).
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

        // Real Tab navigation (Issue #1958 fix_delta iteration 3, blocker 2):
        // never `.focus()` injection -- click an empty area first to
        // establish a known, non-focused baseline, then walk real Tab
        // presses to reach Assist. Deliberately does NOT press Enter/Space
        // here (unlike the dedicated activation test above): this matrix
        // reuses the SAME page/game-session across every zoom cell for a
        // given viewport (see the outer `for (const [, scenarios] of
        // viewportGroups)` loop), so activating Assist here would leak
        // command-intent state into the next cell's `assistStatus` check.
        // Full click/Enter/Space activation semantics are covered once,
        // deterministically, by the dedicated test above.
        await page.locator('body').click({ position: { x: 1, y: 1 } })
        await tabToLocator(page, assistButton, {
          label: `Assist allies (real Tab, ${scenario.viewport.label} ${scenario.zoom.label})`,
        })
        await expect(assistButton).toBeFocused()
        await expect(assistStatus).toHaveText('Assist ready.')
        await assertFocusIndicatorVisible(
          assistButton,
          `Assist allies (${scenario.viewport.label} ${scenario.zoom.label})`,
        )

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
          checked_control: 'assist',
          ...observed,
        })
      }
    } finally {
      await zoomCtx.close()
    }
  }

  // Issue #1958 AC7 (PR #2006 review fix_delta iteration 1, blocker 2): the
  // 375x667 minimum supported viewport (AC2). Edge-control (Weapon/Assist)
  // is intentionally collapsed here, so this exercises the never-collapsing
  // Pause control instead (see `COLLAPSED_EDGE_SCENARIOS`'s doc comment) --
  // same artifact capture (viewport, DPR, zoom, userAgent, head SHA) as the
  // Assist-based scenarios above, individually reviewable per cell.
  const collapsedEdgeGroups = new Map<string, typeof COLLAPSED_EDGE_SCENARIOS>()
  for (const scenario of COLLAPSED_EDGE_SCENARIOS) {
    const key = scenario.viewport.label
    const group = collapsedEdgeGroups.get(key) ?? []
    group.push(scenario)
    collapsedEdgeGroups.set(key, group)
  }

  for (const [, scenarios] of collapsedEdgeGroups) {
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

        const pauseButton = page.locator('[data-action="toggle-pause"]')

        await expect(pauseButton).toBeVisible()
        await expect(pauseButton).toBeEnabled()
        await expect(pauseButton).toHaveText('Pause')
        // AC2/AC1: edge-control is collapsed at this viewport -- confirm
        // that, rather than silently assuming it (a regression here would
        // mean this scenario stopped exercising its intended collapsed
        // state and should instead run through the SCENARIOS/Assist path).
        await expect(page.locator('[data-hud-zone="edge-control"]')).toBeHidden()

        // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 2):
        // deliberately no `scrollIntoViewIfNeeded()` before reading
        // geometry below -- see the equivalent comment in the Assist
        // matrix above.
        const buttonBox = await pauseButton.boundingBox()
        expect(buttonBox).not.toBeNull()
        expect(buttonBox!.x).toBeGreaterThanOrEqual(0)
        expect(buttonBox!.y).toBeGreaterThanOrEqual(0)
        expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(scenario.viewport.width)
        expect(buttonBox!.y + buttonBox!.height).toBeLessThanOrEqual(scenario.viewport.height)

        // Real Tab navigation (Issue #1958 fix_delta iteration 3, blocker 2):
        // never `.focus()` injection. Deliberately does NOT press
        // Enter/Space here for the same same-page-reused-across-cells
        // reason documented in the Assist matrix above (pausing would leak
        // paused state into the next zoom cell) -- full activation
        // semantics are covered once, deterministically, by the dedicated
        // activation test above.
        await page.locator('body').click({ position: { x: 1, y: 1 } })
        await tabToLocator(page, pauseButton, {
          label: `Pause (real Tab, ${scenario.viewport.label} ${scenario.zoom.label})`,
        })
        await expect(pauseButton).toBeFocused()
        await assertFocusIndicatorVisible(pauseButton, `Pause (${scenario.viewport.label} ${scenario.zoom.label})`)

        const screenshotPath = testInfo.outputPath(
          `assist-player-affordance-${scenario.viewport.label}-${scenario.zoom.label.replace('%', 'pct')}-pause.png`,
        )
        await page.screenshot({
          path: screenshotPath,
          fullPage: true,
        })
        expect(existsSync(screenshotPath), `screenshot must exist on disk: ${screenshotPath}`).toBe(true)

        const observedZoom = await zoomCtx.getZoom()
        const observed = await collectEvidence(page)
        expect(observed.logical_arena).toEqual({ width: 960, height: 540 })
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
          checked_control: 'pause',
          ...observed,
        })
      }
    } finally {
      await zoomCtx.close()
    }
  }

  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 4): every
  // recorded evidence entry's head_sha must exactly equal the CI-provided
  // EXPECTED_PR_HEAD_SHA (never merely "was captured/non-empty").
  for (const entry of evidence) {
    expect(entry.head_sha, `evidence head_sha must exactly equal EXPECTED_PR_HEAD_SHA`).toBe(headSha)
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
