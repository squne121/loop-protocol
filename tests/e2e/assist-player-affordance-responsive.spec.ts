import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import os from 'node:os'
import { resolveExpectedHeadSha } from './visual-utils'
import {
  assertPointerMapsToLogicalArena,
  collectEvidence,
  existsSync,
  frozenGameplayState,
  getGameState,
  launchZoomCapableContext,
  waitForRunningWithCombatActors,
  waitForZoomToApply,
  type EvidenceEntry,
} from './assist-player-affordance-helpers'

// ---------------------------------------------------------------------------
// Issue #2119: the responsive viewport x DPR x browser-zoom geometry matrix
// physically split out of `assist-player-affordance.spec.ts` (which was the
// single largest E2E hotspot, ~3.2min of a ~6.5min standard run). This spec
// runs EXCLUSIVELY in the `e2e-responsive-matrix` CI provider job/lane
// (`playwright.config.ts` `LOOP_E2E_LANE=responsive`) and is excluded from
// the `e2e-core` lane. `RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1` (below) is the
// machine contract `scripts/ci/verify-e2e-lane-partition.mjs` and
// `tests/ci/test_playwright_lane_contract.py` verify against real runtime
// evidence: the expected tuple set (viewport x DPR x zoom) must exactly
// equal the recorded runtime evidence tuple set (no missing/extra tuples,
// zero duplicates), and every cell carries PR head SHA, viewport, DPR, zoom,
// pointer-mapping evidence, and frozen-state evidence.
// ---------------------------------------------------------------------------

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

/** RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1 expected tuple count (viewports x DPRs x zooms). */
export const RESPONSIVE_CANVAS_MATRIX_EXPECTED_CELL_COUNT =
  RESPONSIVE_VIEWPORTS.length * RESPONSIVE_DPRS.length * RESPONSIVE_ZOOMS.length

// eslint-disable-next-line no-empty-pattern -- Playwright requires the first arg to be a (possibly empty) fixtures destructure.
test('responsive canvas preserves the logical arena, frozen combat positions, backing store, and pointer mapping across viewport, DPR, and zoom', async ({}, testInfo) => {
  test.setTimeout(300_000)
  const evidence: Array<EvidenceEntry & { declared_dpr: number }> = []
  const headSha = resolveExpectedHeadSha()
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

      // Pass 1 (paused, Issue #1376 AC4 fix): verify canvas geometry and the
      // frozen-combat-position invariant across the full viewport x zoom
      // matrix while the pause dialog is open. AC4 makes the Canvas `inert`
      // while paused, so this pass intentionally performs no pointer
      // input/mapping assertions -- a real OS-level pointer cannot reach an
      // `inert` element, and asserting otherwise would contradict AC4.
      // Pass-1 results are keyed by viewport+zoom and consumed by Pass 2
      // below, which resumes the game to exercise real pointer delivery.
      const frozenResults = new Map<string, ReturnType<typeof frozenGameplayState>>()
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

          frozenResults.set(`${viewport.label}:${zoom.label}`, frozenAfterResize)
        }
      }

      // Pass 2: verify pointer-to-arena coordinate mapping across the same
      // viewport x zoom matrix. Issue #1376 iteration 7 fix: an earlier
      // version of this pass resumed the SAME paused sortie (either once for
      // the whole matrix, or per combo) to let real pointer input reach the
      // Canvas again -- but the underlying combat simulation genuinely ticks
      // while resumed, and empirically the ambient enemy fire always
      // depleted the player's hull to defeat after roughly the same ~10s of
      // cumulative real resumed time, regardless of how that exposure was
      // chunked across combos, well before all 16 combos could be checked.
      // Pointer-to-arena mapping (unlike the frozen-position invariant in
      // Pass 1) has no dependency on pause state at all -- AC4 only
      // constrains input while paused -- so this pass instead starts a
      // FRESH sortie (full hull, `running` phase, Canvas never paused/inert)
      // for every combo via `page.goto('/')`, bounding each combo's combat
      // exposure to just that one combo's check instead of accumulating
      // across the whole matrix.
      for (const viewport of RESPONSIVE_VIEWPORTS) {
        for (const zoom of RESPONSIVE_ZOOMS) {
          await page.goto('/')
          await waitForRunningWithCombatActors(page)

          await page.setViewportSize(viewport)
          await zoomCtx.setZoom(zoom.factor)
          // Issue #1956 responsive-canvas iteration 2 fix: `setZoom()`
          // resolving does not guarantee the zoom has propagated to this
          // page's renderer yet (see `waitForZoomToApply()` doc comment) --
          // wait for the page-observable `devicePixelRatio` to actually
          // reach the expected (declared DPR x zoom factor) value before
          // trusting any subsequent `getBoundingClientRect()` read.
          await waitForZoomToApply(page, dpr * zoom.factor)

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

          // Reset the (virtual) cursor to a known-good position inside the
          // new viewport immediately after every resize/zoom change --
          // otherwise it may still be resting at a coordinate from the
          // previous (larger) viewport that now falls outside the new one,
          // which was observed empirically to desynchronize subsequent
          // page.mouse.move() position tracking across rapid successive
          // viewport/zoom changes in this harness.
          await page.mouse.move(10, 10)

          const pointerMapping = await assertPointerMapsToLogicalArena(page)
          const observed = await collectEvidence(page)
          const observedZoom = await zoomCtx.getZoom()

          expect(observed.logical_arena).toEqual({ width: 960, height: 540 })

          // Fix 6 point 4: every matrix cell gets a real screenshot; no
          // 'not-captured' placeholder path.
          const screenshotPath = testInfo.outputPath(
            `responsive-canvas-dpr${dpr}-${viewport.label}-${zoom.label.replace('%', 'pct')}.png`,
          )
          await page.screenshot({ path: screenshotPath })
          expect(existsSync(screenshotPath), `screenshot must exist on disk: ${screenshotPath}`).toBe(true)

          const frozenAfterResize = frozenResults.get(`${viewport.label}:${zoom.label}`)
          if (!frozenAfterResize) {
            throw new Error(`missing Pass 1 frozen-gameplay result for ${viewport.label}:${zoom.label}`)
          }

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

  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 4): every
  // recorded evidence entry's head_sha must exactly equal the CI-provided
  // EXPECTED_PR_HEAD_SHA.
  for (const entry of evidence) {
    expect(entry.head_sha, `evidence head_sha must exactly equal EXPECTED_PR_HEAD_SHA`).toBe(headSha)
  }

  // RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1 (Issue #2119 AC4): the runtime
  // evidence tuple set (viewport x DPR x zoom) must exactly equal the
  // expected tuple set with zero duplicates -- verified both here (fail the
  // test itself) and independently by
  // `scripts/ci/verify-e2e-lane-partition.mjs` / `tests/ci/` against the
  // written evidence artifact (defense in depth: this in-test assertion
  // fails fast in the E2E run itself; the CI script re-verifies from the
  // artifact so a change to this test file cannot silently drop the
  // independent check).
  expect(evidence.length, 'RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: evidence cell count must equal the expected tuple count').toBe(
    RESPONSIVE_CANVAS_MATRIX_EXPECTED_CELL_COUNT,
  )
  const seenTuples = new Set<string>()
  for (const entry of evidence) {
    const tupleKey = `${entry.viewport}|${entry.declared_dpr}|${entry.browser_zoom}`
    expect(seenTuples.has(tupleKey), `RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: duplicate tuple ${tupleKey}`).toBe(false)
    seenTuples.add(tupleKey)
    expect(entry.pointer_mapping, `RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: ${tupleKey} missing pointer_mapping evidence`).toBeTruthy()
    expect(entry.frozen_gameplay, `RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1: ${tupleKey} missing frozen_gameplay evidence`).toBeTruthy()
  }

  await writeFile(
    testInfo.outputPath('responsive-canvas-runtime-evidence.json'),
    JSON.stringify({
      schema: 'RESPONSIVE_CANVAS_MATRIX_CONTRACT_V1',
      head_sha: headSha,
      matrix: {
        viewports: RESPONSIVE_VIEWPORTS,
        device_scale_factors: RESPONSIVE_DPRS,
        browser_zooms: RESPONSIVE_ZOOMS,
      },
      expected_cell_count: RESPONSIVE_CANVAS_MATRIX_EXPECTED_CELL_COUNT,
      evidence,
    }, null, 2),
    'utf8',
  )
})
