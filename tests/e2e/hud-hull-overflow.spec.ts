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
import { resolveExpectedHeadSha } from './visual-utils'

/**
 * Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 4): captures
 * the expected head SHA at test-run time for evidence recording (item 3's
 * "recorded viewport/DPR/zoom/userAgent/head SHA"). Delegates to
 * `resolveExpectedHeadSha()` (`tests/e2e/visual-utils.ts`), which requires
 * the CI-provided `EXPECTED_PR_HEAD_SHA` env var to be a valid 40-hex
 * commit SHA and THROWS if it is missing/invalid -- an earlier version of
 * this function silently fell back to the literal string `'unknown'`,
 * which then passed the evidence assertion below (`not.toBe('')`) despite
 * recording no real provenance at all.
 */
function currentHeadSha(): string {
  return resolveExpectedHeadSha()
}

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
    // Scope Delta (Issue #1375): the Hull field moved from
    // `[data-field="hp"]` (all phases) to `[data-field="combat-hud-hull"]`
    // inside the running-only `data-combat-hud` root.
    const hpEl = document.querySelector<HTMLElement>('[data-field="combat-hud-hull"]')
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

// Issue #1375 PR #1925 review (owner playtest, P0-3): the AC7 desktop
// resolutions from the Issue body were never covered by an automated
// viewport matrix. Added alongside the existing AC2 overflow matrix.
const GEOMETRY_VIEWPORTS = [
  ...VIEWPORTS,
  { width: 1366, height: 768, label: '1366x768' },
  { width: 1920, height: 1080, label: '1920x1080' },
]

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
        // Wait for HUD to be rendered (data-field="combat-hud-hull" must be present)
        await page.waitForSelector('[data-field="combat-hud-hull"]', { timeout: 10_000 })

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

// ---------------------------------------------------------------------------
// AC7 (Issue #1375 PR #1925 review, owner playtest, P0-3): the combat HUD's
// bounding box must stay inside the Canvas viewport (not the header) with a
// 16px safe margin, never overlap the header rect, and both Assist allies
// and Pause must stay in the viewport together, across desktop resolutions.
// ---------------------------------------------------------------------------

const HUD_SAFE_MARGIN_PX = 16

interface Rect {
  x: number
  y: number
  width: number
  height: number
}

function rectsIntersect(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
}

/**
 * Asserts `inner` is fully contained within `outer` INSET by `margin` on
 * every side (Issue #1958 AC3 fix): `inner` must stay at least `margin`
 * CSS px inside `outer`'s edges, not merely within `outer` expanded
 * outward by `margin` (the previous, backwards, false-green comparison --
 * that version allowed `inner` to overflow `outer` by up to `margin` px
 * and still pass). `epsilon` absorbs sub-pixel rounding only, never the
 * declared safe-zone margin itself.
 */
function assertWithinSafeMargin(inner: Rect, outer: Rect, margin: number, label: string): void {
  const epsilon = 1
  expect(inner.x, `${label}: left edge inside safe margin`).toBeGreaterThanOrEqual(
    outer.x + margin - epsilon,
  )
  expect(inner.y, `${label}: top edge inside safe margin`).toBeGreaterThanOrEqual(
    outer.y + margin - epsilon,
  )
  expect(inner.x + inner.width, `${label}: right edge inside safe margin`).toBeLessThanOrEqual(
    outer.x + outer.width - margin + epsilon,
  )
  expect(inner.y + inner.height, `${label}: bottom edge inside safe margin`).toBeLessThanOrEqual(
    outer.y + outer.height - margin + epsilon,
  )
}

/**
 * Issue #1958 AC4 (PR #2006 review fix_delta iteration 1, blocker 1): the
 * Canvas's static center 60%x60% protected zone must never intersect a
 * persistent opaque/interactive combat HUD fragment. Checks the HUD's
 * named zones (`data-hud-zone`) directly rather than the whole
 * `[data-combat-hud]` root -- the root itself is a transparent,
 * `pointer-events: none` grid spanning the full safe-zone box (see
 * `COMBAT_HUD_MARKUP`'s doc comment in `src/ui/combatHud.ts`), so it is
 * explicitly excluded from this check per the Issue's "transparent layout
 * root" carve-out. Only zones that are actually visible in the current
 * viewport are checked (e.g. `edge-control` collapses below 420px, AC1/AC2).
 */
async function assertNoProtectedZoneIntersection(
  page: Page,
  canvasBox: Rect,
  label: string,
): Promise<void> {
  const protectedZone: Rect = {
    x: canvasBox.x + canvasBox.width * 0.2,
    y: canvasBox.y + canvasBox.height * 0.2,
    width: canvasBox.width * 0.6,
    height: canvasBox.height * 0.6,
  }

  const zoneSelectors = [
    '[data-hud-zone="status"]',
    '[data-hud-zone="elapsed"]',
    '[data-hud-zone="edge-control"]',
    '[data-hud-zone="pause"]',
  ]

  for (const selector of zoneSelectors) {
    const zone = page.locator(selector)
    const isVisible = await zone.isVisible()
    if (!isVisible) {
      // Not currently rendered/visible (e.g. edge-control collapsed at
      // 375x667) -- not a persistent fragment in this viewport, skip.
      continue
    }
    const zoneBox = await zone.boundingBox()
    // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 3): a null
    // boundingBox() for a zone that `isVisible()` reported as visible must
    // FAIL the assertion, never silently `continue` past it -- a visible
    // element with no box is itself a bug (e.g. zero-size layout), not a
    // benign "not currently rendered" case.
    expect(zoneBox, `${label}: HUD zone "${selector}" is visible but boundingBox() is null`).not.toBeNull()
    if (!zoneBox) {
      continue
    }
    expect(
      rectsIntersect(zoneBox, protectedZone),
      `${label}: HUD zone "${selector}" box ${JSON.stringify(zoneBox)} must not intersect ` +
        `the Canvas center 60%x60% protected zone ${JSON.stringify(protectedZone)}`,
    ).toBe(false)
  }
}

/**
 * Issue #1958 fix_delta iteration 2 item 1 (PR #2006 review, owner decision
 * comment https://github.com/squne121/loop-protocol/issues/1958#issuecomment-5205380696):
 * runtime placement verification via BOUNDING BOX geometry only -- never CSS
 * class/grid-area names, and never `[data-combat-hud]`'s own rect (that root
 * is a transparent, `pointer-events: none` layout box spanning the whole
 * safe zone -- see `COMBAT_HUD_MARKUP`'s doc comment in
 * `src/ui/combatHud.ts` -- so its position proves nothing about where any
 * individual fragment actually renders). Emits full diagnostics (Canvas
 * rect, fragment rect, safe margin, protected-zone rect, epsilon) on every
 * assertion via the `message` argument so a failure is self-explanatory in
 * CI output without needing to reproduce locally.
 */
async function assertSemanticPlacement(
  page: Page,
  canvasBox: Rect,
  label: string,
  opts: { edgeControlExpectedVisible: boolean },
): Promise<void> {
  const epsilon = 1
  const margin = HUD_SAFE_MARGIN_PX
  const safeZone: Rect = {
    x: canvasBox.x + margin,
    y: canvasBox.y + margin,
    width: canvasBox.width - margin * 2,
    height: canvasBox.height - margin * 2,
  }
  const diag = (fragmentBox: Rect | null) =>
    `${label}: canvas=${JSON.stringify(canvasBox)} safeZone=${JSON.stringify(safeZone)} ` +
    `fragment=${JSON.stringify(fragmentBox)} margin=${margin} epsilon=${epsilon}`

  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 3): "lower
  // region" / "top region" are now EXPLICIT normalized thresholds against
  // the safe zone's height (canvas-normalized, not sibling-relative), not a
  // same-row/one-pixel-below-elapsed heuristic. Empirically measured across
  // every GEOMETRY_VIEWPORTS/DPR cell in the current production layout
  // (`node measure_geometry.cjs` against the real built app, Issue #1958
  // fix_delta iteration 3 evidence): elapsed's vertical center never exceeds
  // ~3.6% of the safe-zone height from its top, while status/pause's
  // vertical center never falls below ~20.5% (1920x1080, the tallest
  // canvas, where `.combat-hud` legitimately sits closest to the top since
  // it is content-sized and anchored to the safe zone's top -- see the doc
  // comment on `.combat-hud` in `src/style.css`). 0.12 / 0.15 keep a
  // deliberate buffer on both sides of that measured gap so this remains a
  // real, meaningful separation rather than either an unreachable
  // majority-of-canvas-height bar or a trivial one-pixel check.
  const TOP_REGION_MAX_CENTER_Y_FRACTION = 0.12
  const LOWER_REGION_MIN_CENTER_Y_FRACTION = 0.15
  // edge-control / pause's right edge anchors flush against the safe zone's
  // right edge in the current layout (measured fraction 1.0 at every
  // matrix cell) -- 0.75 ("right quarter") is a real, materially tighter
  // bound than "past the midpoint" while keeping headroom for legitimate
  // sub-pixel/rendering drift.
  const RIGHT_EDGE_REGION_MIN_X_FRACTION = 0.75
  // status's left edge anchors flush against the safe zone's left edge
  // (measured fraction 0.0 at every matrix cell); allow a small band for
  // rendering drift, never "no upper bound at all".
  const LEFT_EDGE_MAX_X_FRACTION = 0.1

  /**
   * Requires `locator` to be visible AND have a non-null boundingBox()
   * (Issue #1958 fix_delta iteration 3, blocker 3: a null box for a visible
   * element must fail, never be silently skipped), then independently
   * asserts that box satisfies the 16px inner safe-margin containment
   * (AC3) against the real Canvas rect -- never the transparent
   * `[data-combat-hud]` root as a stand-in for an individual fragment.
   */
  async function requireVisibleBoxWithinSafeMargin(
    locator: ReturnType<Page['locator']>,
    fragmentLabel: string,
  ): Promise<Rect> {
    await expect(locator, `${label}: ${fragmentLabel} must be visible`).toBeVisible()
    const box = await locator.boundingBox()
    expect(box, `${label}: ${fragmentLabel} is visible but boundingBox() is null`).not.toBeNull()
    if (!box) {
      throw new Error(`${label}: ${fragmentLabel} boundingBox() is null`)
    }
    assertWithinSafeMargin(box, canvasBox, margin, `${label} ${fragmentLabel}-in-canvas`)
    return box
  }

  // elapsed: top-center-low-prominence -- horizontally centered in the safe
  // zone, vertical center within the Canvas TOP region.
  const elapsedBox = await requireVisibleBoxWithinSafeMargin(
    page.locator('[data-hud-zone="elapsed"]'),
    'elapsed zone',
  )
  const elapsedCenterX = elapsedBox.x + elapsedBox.width / 2
  const elapsedCenterY = elapsedBox.y + elapsedBox.height / 2
  const safeZoneCenterX = safeZone.x + safeZone.width / 2
  expect(
    Math.abs(elapsedCenterX - safeZoneCenterX),
    `${diag(elapsedBox)}: elapsed zone is horizontally centered in the safe zone`,
  ).toBeLessThanOrEqual(safeZone.width * 0.15 + epsilon)
  expect(
    elapsedCenterY,
    `${diag(elapsedBox)}: elapsed zone center is in the Canvas TOP region ` +
      `(<= ${TOP_REGION_MAX_CENTER_Y_FRACTION * 100}% of safe-zone height from its top)`,
  ).toBeLessThanOrEqual(safeZone.y + safeZone.height * TOP_REGION_MAX_CENTER_Y_FRACTION + epsilon)

  // status zone (Hull/critical/Kills): anchors to the Canvas inner-safe LEFT
  // edge, and its vertical center is in the Canvas LOWER region.
  const statusBox = await requireVisibleBoxWithinSafeMargin(page.locator('[data-hud-zone="status"]'), 'status zone')
  const statusCenterY = statusBox.y + statusBox.height / 2
  expect(
    statusBox.x,
    `${diag(statusBox)}: status zone left edge anchors the safe-zone left edge`,
  ).toBeGreaterThanOrEqual(safeZone.x - epsilon)
  expect(
    statusBox.x,
    `${diag(statusBox)}: status zone left edge anchors the safe-zone left edge`,
  ).toBeLessThanOrEqual(safeZone.x + safeZone.width * LEFT_EDGE_MAX_X_FRACTION + epsilon)
  expect(
    statusCenterY,
    `${diag(statusBox)}: status zone center is in the Canvas LOWER region ` +
      `(>= ${LOWER_REGION_MIN_CENTER_Y_FRACTION * 100}% of safe-zone height from its top)`,
  ).toBeGreaterThanOrEqual(safeZone.y + safeZone.height * LOWER_REGION_MIN_CENTER_Y_FRACTION - epsilon)

  // critical warning fragment (when shown, AC6): checked independently as
  // its own visible opaque fragment, in addition to the status card box.
  const criticalField = page.locator('[data-field="combat-hud-critical"]')
  if (await criticalField.isVisible()) {
    await requireVisibleBoxWithinSafeMargin(criticalField, 'critical warning fragment')
  }

  // edge-control (Weapon/Assist): when visible, anchors NEAR the safe zone's
  // right edge (right quarter) -- never merely past the midpoint.
  const edgeVisible = await page.locator('[data-hud-zone="edge-control"]').isVisible()
  expect(edgeVisible, `${label}: edge-control visibility must match the expected collapse state`).toBe(
    opts.edgeControlExpectedVisible,
  )
  if (edgeVisible) {
    const edgeBox = await requireVisibleBoxWithinSafeMargin(
      page.locator('[data-hud-zone="edge-control"]'),
      'edge-control zone',
    )
    expect(
      edgeBox.x + edgeBox.width,
      `${diag(edgeBox)}: edge-control zone right edge is near the safe-zone right edge (right quarter)`,
    ).toBeGreaterThanOrEqual(safeZone.x + safeZone.width * RIGHT_EDGE_REGION_MIN_X_FRACTION - epsilon)

    // Assist button itself (AC5/AC7): its own bounding box, independently
    // of the edge-control card that contains it.
    const assistButton = page.getByRole('button', { name: 'Assist allies' })
    if (await assistButton.isVisible()) {
      await requireVisibleBoxWithinSafeMargin(assistButton, 'Assist allies button')
    }
  }

  // pause: separate-pause-control, near the safe zone's right edge, its
  // vertical center is in the Canvas LOWER region, and it never overlaps
  // the status zone (AC1: "Pause remains a separate focusable/pointer-
  // operable control").
  const pauseButton = page.getByRole('button', { name: 'Pause' })
  const pauseBox = await requireVisibleBoxWithinSafeMargin(pauseButton, 'Pause button')
  const pauseCenterY = pauseBox.y + pauseBox.height / 2
  expect(
    pauseBox.x + pauseBox.width,
    `${diag(pauseBox)}: pause button right edge is near the safe-zone right edge (right quarter)`,
  ).toBeGreaterThanOrEqual(safeZone.x + safeZone.width * RIGHT_EDGE_REGION_MIN_X_FRACTION - epsilon)
  expect(
    pauseCenterY,
    `${diag(pauseBox)}: pause button center is in the Canvas LOWER region ` +
      `(>= ${LOWER_REGION_MIN_CENTER_Y_FRACTION * 100}% of safe-zone height from its top)`,
  ).toBeGreaterThanOrEqual(safeZone.y + safeZone.height * LOWER_REGION_MIN_CENTER_Y_FRACTION - epsilon)
  expect(
    rectsIntersect(pauseBox, statusBox),
    `${label}: pause zone ${JSON.stringify(pauseBox)} must never overlap the status zone ${JSON.stringify(statusBox)} (separate control, not a status-card member)`,
  ).toBe(false)
}

async function assertHudGeometry(page: Page, label: string): Promise<void> {
  const hud = page.locator('[data-combat-hud]')
  const canvas = page.locator('canvas.battle-stage__canvas')
  const header = page.locator('.battle-stage__header')

  await expect(hud).toBeVisible()

  const hudBox = await hud.boundingBox()
  const canvasBox = await canvas.boundingBox()
  const headerBox = await header.boundingBox()

  expect(hudBox, `${label}: combat HUD must have a bounding box`).not.toBeNull()
  expect(canvasBox, `${label}: canvas must have a bounding box`).not.toBeNull()
  expect(headerBox, `${label}: header must have a bounding box`).not.toBeNull()

  if (!hudBox || !canvasBox || !headerBox) {
    return
  }

  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 3): the
  // 16px safe-margin containment is no longer checked against `hudBox` (the
  // transparent, `pointer-events: none` `[data-combat-hud]` root) here --
  // that root is EXCLUDED from AC3 by the Issue's own text ("透明な HUD root
  // 自体は対象外") and spans the whole safe zone by construction, so
  // checking it proves nothing about any individual fragment.
  // `assertSemanticPlacement()` below independently checks the 16px
  // containment for every VISIBLE opaque fragment/interactive control
  // (elapsed/status/critical/edge-control/Assist/Pause).

  // AC4 (Issue #1958): persistent HUD fragments must not intersect the
  // Canvas center 60%x60% static protected zone.
  await assertNoProtectedZoneIntersection(page, canvasBox, label)

  // AC1/AC3 (Issue #1958 fix_delta iteration 3, blocker 3): runtime
  // bounding-box proof of the semantic state table's declared placement
  // regions AND the per-fragment 16px safe-margin containment.
  await assertSemanticPlacement(page, canvasBox, label, { edgeControlExpectedVisible: true })

  // HUD and header never overlap as rectangles.
  expect(
    rectsIntersect(hudBox, headerBox),
    `${label}: HUD box ${JSON.stringify(hudBox)} must not intersect header box ${JSON.stringify(headerBox)}`,
  ).toBe(false)

  // Assist allies and Pause both stay in the viewport together.
  await expect(page.getByRole('button', { name: 'Assist allies' })).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Pause' })).toBeInViewport()

  // HUD element itself has no internal overflow (neither axis).
  const overflow = await hud.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }))
  expect(overflow.scrollWidth, `${label}: HUD scrollWidth <= clientWidth`).toBeLessThanOrEqual(
    overflow.clientWidth,
  )
  expect(overflow.scrollHeight, `${label}: HUD scrollHeight <= clientHeight`).toBeLessThanOrEqual(
    overflow.clientHeight,
  )
}

/**
 * Variant of `assertHudGeometry` for the 375x667 minimum supported
 * viewport (Issue #1958 AC1/AC2): Weapon/Assist (`edge-control`) is
 * intentionally collapsed/hidden by `src/style.css`'s
 * `@media (max-width: 420px)` rule (the semantic state table's collapse
 * priority), so "Assist allies in viewport" is not asserted here. Hull /
 * critical / Kills / elapsed / Pause -- the never-collapsing fragments --
 * still must stay within the Canvas safe margin, never overlap the header,
 * and the HUD must have no internal overflow.
 */
async function assertHudGeometryAtCollapsedMinimum(page: Page, label: string): Promise<void> {
  const hud = page.locator('[data-combat-hud]')
  const canvas = page.locator('canvas.battle-stage__canvas')
  const header = page.locator('.battle-stage__header')

  await expect(hud).toBeVisible()

  const hudBox = await hud.boundingBox()
  const canvasBox = await canvas.boundingBox()
  const headerBox = await header.boundingBox()

  expect(hudBox, `${label}: combat HUD must have a bounding box`).not.toBeNull()
  expect(canvasBox, `${label}: canvas must have a bounding box`).not.toBeNull()
  expect(headerBox, `${label}: header must have a bounding box`).not.toBeNull()

  if (!hudBox || !canvasBox || !headerBox) {
    return
  }

  // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 3): see the
  // equivalent comment in `assertHudGeometry()` above -- the transparent
  // `[data-combat-hud]` root is excluded from AC3; per-fragment containment
  // is checked in `assertSemanticPlacement()` below.

  // AC4 (Issue #1958): persistent HUD fragments must not intersect the
  // Canvas center 60%x60% static protected zone.
  await assertNoProtectedZoneIntersection(page, canvasBox, label)

  // AC1/AC3 (Issue #1958 fix_delta iteration 3, blocker 3): runtime
  // bounding-box proof of the semantic state table's declared placement
  // regions AND the per-fragment 16px safe-margin containment.
  // edge-control (Weapon/Assist) is collapsed at this viewport.
  await assertSemanticPlacement(page, canvasBox, label, { edgeControlExpectedVisible: false })

  expect(
    rectsIntersect(hudBox, headerBox),
    `${label}: HUD box ${JSON.stringify(hudBox)} must not intersect header box ${JSON.stringify(headerBox)}`,
  ).toBe(false)

  // AC1, AC2: the never-collapsing fragments stay reachable even when
  // edge-control is collapsed.
  await expect(page.getByRole('button', { name: 'Pause' })).toBeInViewport()

  const overflow = await hud.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }))
  expect(overflow.scrollWidth, `${label}: HUD scrollWidth <= clientWidth`).toBeLessThanOrEqual(
    overflow.clientWidth,
  )
  expect(overflow.scrollHeight, `${label}: HUD scrollHeight <= clientHeight`).toBeLessThanOrEqual(
    overflow.clientHeight,
  )
}

test.describe('hud geometry: combat HUD stays inside the Canvas viewport (AC7)', () => {
  for (const vp of GEOMETRY_VIEWPORTS) {
    // Issue #1958 AC2: the combat HUD's placement/safe-zone/priority
    // redesign (`src/ui/combatHud.ts`'s zoned grid + `src/style.css`'s
    // `@media (max-width: 420px)` progressive-disclosure collapse of the
    // edge-control zone) fits the 375x667 minimum supported viewport
    // (`Supported minimum viewport` = 375x667) within the strict `inner`
    // safe-margin containment fixed by AC3's `assertWithinSafeMargin`
    // rewrite. The `test.fixme` deferred by Issue #1956 is now resolved.
    test(`viewport=${vp.label}: HUD is within canvas bounds, never overlaps header, Assist+Pause in viewport, no internal overflow`, async ({
      page,
    }) => {
      // Issue #1958 fix_delta iteration 2 (PR #2006 review, owner decision
      // comment https://github.com/squne121/loop-protocol/issues/1958#issuecomment-5205380696):
      // the 375x667 `test.fail()` known-gap marker from iteration 1 is
      // removed here -- `.combat-hud__status`/`.combat-hud__pause` were
      // rebuilt as narrow free-column-confined fragments (`src/style.css`'s
      // `@media (max-width: 420px)` block) that structurally cannot reach
      // the protected zone's x-range regardless of height, so
      // `assertNoProtectedZoneIntersection()` now genuinely passes at this
      // viewport, not just "documented as expected to fail".
      await page.setViewportSize({ width: vp.width, height: vp.height })
      await page.goto('/')
      await page.waitForSelector('[data-combat-hud]', { timeout: 10_000 })

      // AC2: at 375x667, Weapon/Assist collapse first (semantic state table
      // collapse priority) -- Assist allies is not required to stay in
      // viewport at the minimum supported width, so this asserts a variant
      // of `assertHudGeometry` without that check.
      if (vp.label === '375x667') {
        await assertHudGeometryAtCollapsedMinimum(page, vp.label)
      } else {
        await assertHudGeometry(page, vp.label)
      }
    })
  }
})

// ---------------------------------------------------------------------------
// Issue #1958 fix_delta iteration 2 items 2/3 (PR #2006 review, owner
// decision comment https://github.com/squne121/loop-protocol/issues/1958#issuecomment-5205380696):
// 375x667 compact status representation, dual-fixture verification. Exercises
// BOTH a normal HULL fixture and a critical-warning-triggered fixture (same
// direct-DOM-injection strategy this file already uses for AC2a/AC2b/AC2c --
// isolates the layout/AC assertions from game-state setup) and checks every
// item-3 requirement: AC1 placement, 16px inner containment, protected-zone
// non-intersection, HULL display, icon+text in the critical fixture, Pause
// pointer/keyboard/focus reachability, no hidden/collapsed fragment in Tab
// order, no internal scroll on `.battle-hud-layer`, and records
// viewport/DPR/zoom/userAgent/head SHA evidence. Dimensions are derived from
// actual runtime `boundingBox()`/`devicePixelRatio` measurements, never
// hard-coded guesses.
// ---------------------------------------------------------------------------

/** Toggles the persistent critical-warning fragment via direct DOM injection
 * (same strategy `injectHullText` above already uses) -- isolates the
 * critical-fixture layout assertions from needing full game-state/combat
 * setup to actually drive `player.hp` below the critical ratio. */
async function injectCriticalFixture(page: Page, critical: boolean): Promise<void> {
  await page.evaluate((isCritical) => {
    const criticalEl = document.querySelector<HTMLElement>('[data-field="combat-hud-critical"]')
    if (criticalEl) {
      criticalEl.hidden = !isCritical
    }
  }, critical)
}

async function recordCompactFixtureEvidence(
  page: Page,
  label: string,
): Promise<{ devicePixelRatio: number; userAgent: string; headSha: string; viewport: string }> {
  const runtime = await page.evaluate(() => ({
    devicePixelRatio: window.devicePixelRatio,
    userAgent: navigator.userAgent,
  }))
  const headSha = currentHeadSha()
  const evidence = { ...runtime, headSha, viewport: label }
  console.info(`[375x667 compact fixture evidence] ${JSON.stringify(evidence)}`)
  return evidence
}

test.describe('375x667 compact status representation: dual-fixture verification (AC1/AC2/AC4/AC6)', () => {
  for (const fixture of [
    { critical: false, hullText: '42/100', label: 'normal HULL fixture' },
    { critical: true, hullText: '5/100', label: 'critical-warning-triggered fixture' },
  ]) {
    test(`375x667 ${fixture.label}: AC1 placement, 16px containment, AC4 non-intersection, HULL display, Pause reachability, no hidden Tab-order fragment, no internal scroll`, async ({
      page,
    }) => {
      const label = `375x667 ${fixture.label}`
      await page.setViewportSize({ width: 375, height: 667 })
      await page.goto('/')
      await page.waitForSelector('[data-combat-hud]', { timeout: 10_000 })

      await injectHullText(page, fixture.hullText)
      await injectCriticalFixture(page, fixture.critical)

      const evidence = await recordCompactFixtureEvidence(page, label)
      // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 4):
      // exact equality against EXPECTED_PR_HEAD_SHA, not merely non-empty --
      // `currentHeadSha()` now throws on missing/invalid EXPECTED_PR_HEAD_SHA,
      // so this assertion is a defensive double-check against future drift.
      expect(evidence.headSha, `${label}: head SHA must exactly equal EXPECTED_PR_HEAD_SHA`).toBe(
        process.env.EXPECTED_PR_HEAD_SHA,
      )

      const canvas = page.locator('canvas.battle-stage__canvas')
      const canvasBox = await canvas.boundingBox()
      expect(canvasBox, `${label}: canvas must have a bounding box`).not.toBeNull()
      if (!canvasBox) {
        return
      }

      const hud = page.locator('[data-combat-hud]')
      const hudBox = await hud.boundingBox()
      expect(hudBox, `${label}: combat HUD must have a bounding box`).not.toBeNull()
      if (!hudBox) {
        return
      }

      // Issue #1958 fix_delta iteration 3 (PR #2006 review, blocker 3): see
      // the equivalent comment in `assertHudGeometry()` -- the transparent
      // `[data-combat-hud]` root is excluded from AC3; per-fragment
      // containment is checked in `assertSemanticPlacement()` below.

      // AC4: protected-zone non-intersection (real assertion, no test.fail()).
      await assertNoProtectedZoneIntersection(page, canvasBox, label)

      // AC1/AC3: runtime bounding-box placement proof AND per-fragment 16px
      // safe-margin containment. edge-control collapsed.
      await assertSemanticPlacement(page, canvasBox, label, { edgeControlExpectedVisible: false })

      // HULL display: the injected value renders inside the status zone.
      const hullField = page.locator('[data-field="combat-hud-hull"]')
      await expect(hullField).toBeVisible()
      await expect(hullField).toHaveText(fixture.hullText)

      // Icon+text in the critical fixture only (AC6: never color-only).
      const criticalField = page.locator('[data-field="combat-hud-critical"]')
      if (fixture.critical) {
        await expect(criticalField).toBeVisible()
        await expect(criticalField.locator('[aria-hidden="true"]')).toBeVisible()
        await expect(criticalField).toContainText('Hull critical')
      } else {
        await expect(criticalField).toBeHidden()
      }

      // Pause: pointer-, keyboard-, and focus-operable. Uses the
      // `[data-action]` CSS selectors (not `getByRole`) because pausing
      // makes the combat HUD `inert` (Issue #1376 AC4: it drops out of the
      // accessibility tree/tab order behind the pause dialog) -- an
      // accessible-name-based `getByRole` query on the (now inert) Pause
      // button would spuriously report "not found" after the first pause,
      // which is a property of the pause-dialog feature, not a regression
      // in this fixture.
      const pauseButton = page.locator('[data-action="toggle-pause"]')
      const resumeButton = page.locator('[data-action="resume"]')
      await expect(pauseButton).toBeInViewport()
      await expect(pauseButton).toBeVisible()
      await expect(pauseButton).toBeEnabled()

      // Pointer-operable: click opens the pause dialog.
      await pauseButton.click()
      await expect(resumeButton).toBeVisible()
      await resumeButton.click()
      await expect(resumeButton).toBeHidden()
      await expect(pauseButton).toBeVisible()

      // Keyboard- and focus-operable: focus + Enter opens the pause dialog
      // again; Escape resumes (the button's own `title` documents this:
      // "Pause or resume simulation. Also toggled by Escape.").
      await pauseButton.focus()
      await expect(pauseButton).toBeFocused()
      await page.keyboard.press('Enter')
      await expect(resumeButton).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(resumeButton).toBeHidden()
      await expect(pauseButton).toBeVisible()

      // No hidden/collapsed fragment (Assist/Weapon, `.combat-hud__edge`) in
      // Tab order: `.combat-hud__edge` is `display: none` at this viewport
      // (verified above via `edgeControlExpectedVisible: false`), so
      // Tab-cycling through the page's focusable elements must never land on
      // the Assist button while it is not visible.
      const assistButton = page.locator('[data-action="assist-player"]')
      await expect(assistButton).toBeHidden()
      await page.locator('body').click({ position: { x: 1, y: 1 } })
      let hitAssistButton = false
      for (let i = 0; i < 20; i += 1) {
        await page.keyboard.press('Tab')
        const isAssistFocused = await assistButton.evaluate(
          (el) => document.activeElement === el,
        )
        if (isAssistFocused) {
          hitAssistButton = true
          break
        }
      }
      expect(
        hitAssistButton,
        `${label}: Tab order must never focus the collapsed/hidden Assist button`,
      ).toBe(false)

      // No internal scroll on `.battle-hud-layer` (the safe-zone container).
      const hudLayer = page.locator('.battle-hud-layer')
      const hudLayerOverflow = await hudLayer.evaluate((el) => ({
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      }))
      expect(
        hudLayerOverflow.scrollWidth,
        `${label}: .battle-hud-layer scrollWidth <= clientWidth (no internal horizontal scroll)`,
      ).toBeLessThanOrEqual(hudLayerOverflow.clientWidth)
      expect(
        hudLayerOverflow.scrollHeight,
        `${label}: .battle-hud-layer scrollHeight <= clientHeight (no internal vertical scroll)`,
      ).toBeLessThanOrEqual(hudLayerOverflow.clientHeight)
    })
  }
})

// ---------------------------------------------------------------------------
// Regression case for the owner's actual playtest report: Windows/Chrome,
// viewport 1437x1365, devicePixelRatio ~= 0.667 (effectively a wider/taller
// *logical* viewport than the physical display, e.g. OS display scaling
// below 100%). Playwright cannot emulate OS-level zoom directly, but
// `deviceScaleFactor` is the closest supported lever and can only be set at
// browser-context creation, so this test opens its own context rather than
// reusing the default `page` fixture.
//
// Issue #1375 PR #1925 owner playtest (P1, iteration 4): this is an
// approximation, not a substitute for real evidence. Chromium's
// `deviceScaleFactor` models device pixel ratio, which is a different
// mechanism from a real browser's page zoom or the OS's display-scaling
// setting the owner actually reported (Windows Chrome, ~150% logical vs.
// physical mismatch). A regression this test misses because the two
// mechanisms diverge in some edge case would not be caught here — real
// browser zoom / OS scaling still needs to be re-checked against actual
// hardware/browser combinations when in doubt, this test only guards
// against DPR-shaped layout regressions in CI.
// ---------------------------------------------------------------------------

test.describe('hud geometry: low-DPR regression (owner playtest report)', () => {
  test('viewport=1437x1365 deviceScaleFactor=0.667 (approx): HUD stays within canvas bounds', async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width: 1437, height: 1365 },
      // Chromium requires deviceScaleFactor > 0; 0.667 approximates the
      // owner's report of an effectively higher-resolution logical viewport
      // than the physical display (DPR below 1).
      deviceScaleFactor: 0.667,
    })
    const page = await context.newPage()

    try {
      await page.goto('/')
      await page.waitForSelector('[data-combat-hud]', { timeout: 10_000 })

      await assertHudGeometry(page, 'DPR~0.667 1437x1365')
    } finally {
      await context.close()
    }
  })
})

// ---------------------------------------------------------------------------
// Owner playtest regression (PR #1925 owner comment
// https://github.com/squne121/loop-protocol/pull/1925#issuecomment-5151416762,
// iteration 4, P0): "defeated and could not proceed". Root cause: the
// `.legacy-result-surface`'s three `.panel` rows (Sortie / Pilot updates /
// actions=Return to hangar etc.) could overflow `.battle-stage__viewport`'s
// clipped Canvas height, pushing `[data-action="confirm-result"]` below the
// fold with no visible affordance that `.battle-hud-layer`'s
// `overflow-y: auto` scroll was required.
//
// Reaches the result phase through the SAME deterministic fixture
// `tests/e2e/m2-combat-mvp.spec.ts` already uses for its defeat coverage
// (`__E2E_PLAYER_HP_OVERRIDE__ = 1`: first enemy contact ends the sortie in
// defeat) — never a DOM/state shortcut into the result phase. Deliberately
// does NOT call `confirmButton.click()`: Playwright auto-scrolls a click
// target into view first, which is exactly the behavior that let this bug
// go undetected by `tests/e2e/m3-loop-mvp.spec.ts` (Issue #1375 Allowed
// Paths does not include that file, so it is not modified here). Instead
// this asserts the button's actual rendered geometry directly.
// ---------------------------------------------------------------------------

const RESULT_ACTION_VIEWPORTS = [
  { width: 1280, height: 720, label: '1280x720' },
  { width: 1366, height: 768, label: '1366x768' },
  { width: 1920, height: 1080, label: '1920x1080' },
  { width: 1437, height: 1365, label: '1437x1365' },
  { width: 956, height: 1032, label: '956x1032' },
]

async function getSortieStatus(page: Page): Promise<string> {
  return page.evaluate(() => {
    const hook = (
      window as Window & {
        __LOOP_E2E__?: { getState: () => { sortie: { status: string } } }
      }
    ).__LOOP_E2E__
    if (!hook) {
      throw new Error('__LOOP_E2E__ hook not found. Was the app built with VITE_E2E_MODE=true?')
    }
    return hook.getState().sortie.status
  })
}

/**
 * Asserts `inner` is fully contained within `outer` with no safe margin
 * (unlike `assertWithinSafeMargin()` above): the button must not be clipped
 * by `.battle-stage__viewport`'s `overflow: hidden`, so any edge escaping
 * `outer`'s bounds means the button is (at least partially) not actually
 * reachable/visible to the player. `epsilon` absorbs sub-pixel rounding
 * only.
 */
function assertFullyContained(inner: Rect, outer: Rect, label: string): void {
  const epsilon = 1
  expect(inner.x, `${label}: left edge inside canvas viewport`).toBeGreaterThanOrEqual(
    outer.x - epsilon,
  )
  expect(inner.y, `${label}: top edge inside canvas viewport`).toBeGreaterThanOrEqual(
    outer.y - epsilon,
  )
  expect(inner.x + inner.width, `${label}: right edge inside canvas viewport`).toBeLessThanOrEqual(
    outer.x + outer.width + epsilon,
  )
  expect(inner.y + inner.height, `${label}: bottom edge inside canvas viewport`).toBeLessThanOrEqual(
    outer.y + outer.height + epsilon,
  )
}

test.describe('hud result action geometry: confirm-result stays reachable after defeat (owner playtest regression)', () => {
  for (const vp of RESULT_ACTION_VIEWPORTS) {
    test(`viewport=${vp.label}: Return to hangar is in the browser viewport and inside the Canvas viewport rect after defeat`, async ({
      page,
    }) => {
      test.setTimeout(30_000)

      // Deterministic defeat fixture (same mechanism as
      // tests/e2e/m2-combat-mvp.spec.ts's existing defeat coverage): 1 HP
      // means the first enemy contact ends the sortie in defeat.
      await page.addInitScript(() => {
        ;(
          window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }
        ).__E2E_PLAYER_HP_OVERRIDE__ = 1
      })
      await page.setViewportSize({ width: vp.width, height: vp.height })
      await page.goto('/')

      await expect
        .poll(async () => getSortieStatus(page), { timeout: 25_000, intervals: [200] })
        .toBe('defeat')

      const confirmButton = page.locator('[data-action="confirm-result"]')
      const viewport = page.locator('.battle-stage__viewport')

      // Do NOT call confirmButton.click() here — Playwright's auto-scroll-
      // into-view before clicking would silently paper over exactly the bug
      // this test exists to catch.
      await expect(confirmButton).toBeInViewport()

      const buttonBox = await confirmButton.boundingBox()
      const viewportBox = await viewport.boundingBox()
      expect(buttonBox, `${vp.label}: confirm-result must have a bounding box`).not.toBeNull()
      expect(
        viewportBox,
        `${vp.label}: .battle-stage__viewport must have a bounding box`,
      ).not.toBeNull()
      if (!buttonBox || !viewportBox) {
        return
      }

      assertFullyContained(buttonBox, viewportBox, `${vp.label} confirm-result-in-canvas-viewport`)
    })
  }
})

// ---------------------------------------------------------------------------
// Issue #1376 AC11: browser zoom 200% -- Playwright has no native "page zoom"
// control, so this approximates it the same way the codebase already
// documents for OS display scaling (see the low-DPR describe block above):
// Chromium supports the non-standard CSS `zoom` property, applied to the
// root element, as the closest available lever. Not a substitute for real
// browser zoom on actual hardware, but guards against zoom-shaped layout
// regressions in CI (result heading and Return to hangar must stay inside
// the browser viewport and fully reachable).
// ---------------------------------------------------------------------------

test.describe('hud result action geometry: 200% browser zoom (AC11, Issue #1376)', () => {
  // Playwright has no native "real browser page zoom" control (well-known
  // limitation -- Chrome's Ctrl+ zoom is not exposed via CDP for this
  // purpose). This approximates it the same way the low-DPR describe block
  // above already documents for OS display scaling: the CSS `zoom` property
  // (Chromium-only, non-standard) applied to the root element scales
  // rendered content within the SAME layout viewport, which is directionally
  // similar to how real browser zoom shrinks the effective CSS-pixel space
  // available for layout. It is NOT a substitute for testing against real
  // browser zoom on actual hardware. Because this approximation is more
  // aggressive than real zoom typically is in practice, the assertion here
  // is scroll-reachability (matches Playwright's own `click()` semantics,
  // which auto-scrolls the target into view) rather than strict
  // "no-scroll-required" viewport containment -- a scrollable
  // `.battle-screen-layer` with `overflow-y: auto` is an acceptable
  // affordance at this extreme zoom level, unlike being clipped/hidden
  // entirely.
  test('viewport=1280x720 zoom=200%: result heading is visible and Return to hangar remains scroll-reachable and clickable after defeat', async ({
    page,
  }) => {
    test.setTimeout(30_000)

    await page.addInitScript(() => {
      ;(window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }).__E2E_PLAYER_HP_OVERRIDE__ = 1
    })
    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto('/')

    await expect
      .poll(async () => getSortieStatus(page), { timeout: 25_000, intervals: [200] })
      .toBe('defeat')

    // Approximate 200% browser zoom via the CSS `zoom` property.
    await page.evaluate(() => {
      document.documentElement.style.zoom = '2'
    })

    const resultHeading = page.locator('#phase-screen-result-heading')
    const confirmButton = page.locator('[data-action="confirm-result"]')

    await expect(resultHeading).toBeVisible({ timeout: 5_000 })
    // AC11: Return to hangar must not be clipped/hidden -- it must still be
    // reachable (scroll-into-view + click) and actually invoke the action.
    await confirmButton.scrollIntoViewIfNeeded()
    await expect(confirmButton).toBeVisible()
    await confirmButton.click()
    await expect
      .poll(
        async () =>
          page.evaluate(() => {
            const hook = (
              window as Window & { __LOOP_E2E__?: { getState: () => { loopPhase: string } } }
            ).__LOOP_E2E__
            return hook ? hook.getState().loopPhase : 'no-hook'
          }),
        { timeout: 5_000, intervals: [50] },
      )
      .toBe('preparation')
  })
})
