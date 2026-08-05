/**
 * visual-utils.ts — VRT fixture and screenshot-target-guard helpers (Issue #1385).
 *
 * This module is a Playwright E2E helper (imports `@playwright/test` types
 * directly) and is deliberately test-only: it is never imported from `src/`.
 * `VisualScenarioFixture` and `VisualScenarioViewportLabel` are type-only
 * imported FROM `src/main.ts` (single source of truth, Issue #1385 review
 * additional指摘9) — `src/main.ts` never imports this module back, so the
 * production bundle never depends on it, but `pnpm typecheck:e2e` catches
 * drift between the fixture contract and this test helper.
 */

import { expect, type Locator, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import type { VisualScenarioFixture, VisualScenarioViewportLabel } from '../../src/main'

export type { VisualScenarioFixture, VisualScenarioViewportLabel }

// ---------------------------------------------------------------------------
// Scenario Support Matrix (Issue #1385 contract)
// ---------------------------------------------------------------------------

export type VisualScenarioName = VisualScenarioFixture['name']

export const VISUAL_SCENARIOS: readonly VisualScenarioName[] = [
  'running-hud',
  'running-hud-paused',
  'result-timeout',
  'final-overlay-only',
]

export type VisualScenarioFixtureStatus = 'active-fixture-only' | 'pending-fixture'

/**
 * `active-fixture-only`: the scenario's fixture data may be installed and
 * captured by `expectDomOverlayScreenshot()` / `expectCanvasVisualCueScreenshot()`.
 * `pending-fixture`: the scenario name is reserved only. Its overlay
 * surface implementation child issue has not merged yet
 * (`docs/dev/visual-baseline-registry.md`). Both `installVisualScenario()`
 * and the screenshot helpers fail closed for these scenarios rather than
 * silently freezing the current pre-overlay UI (e.g. the legacy
 * `.command-rail`) as a baseline.
 *
 * This `Record<VisualScenarioName, ...>` is exhaustive over the
 * `src/main.ts`-derived `VisualScenarioName` union: adding a new scenario
 * name there without an entry here is a compile error.
 */
export const VISUAL_SCENARIO_STATUS: Readonly<Record<VisualScenarioName, VisualScenarioFixtureStatus>> = {
  'running-hud': 'active-fixture-only',
  // Issue #1376 AC12: pause overlay / result screen surfaces merged in this
  // PR, so these two scenarios are promoted from 'pending-fixture' to
  // 'active-fixture-only'. 'final-overlay-only' remains pending
  // ('final' command-rail removal is #1377's Out of Scope boundary).
  'running-hud-paused': 'active-fixture-only',
  'result-timeout': 'active-fixture-only',
  'final-overlay-only': 'pending-fixture',
}

export function isPendingFixtureScenario(scenario: VisualScenarioName): boolean {
  return VISUAL_SCENARIO_STATUS[scenario] === 'pending-fixture'
}

// ---------------------------------------------------------------------------
// Freeze CSS path (AC1, AC2)
// ---------------------------------------------------------------------------

/** Absolute path to the shared freeze CSS, for Playwright `toHaveScreenshot({ stylePath })`. */
export const VISUAL_FREEZE_CSS_PATH = fileURLToPath(new URL('./visual.freeze.css', import.meta.url))

// ---------------------------------------------------------------------------
// Deterministic viewport labels (AC3 — "viewport label")
// ---------------------------------------------------------------------------

/**
 * Named viewport sizes referenced by `VisualScenarioFixture.viewportLabel`.
 * Typed `Record<VisualScenarioViewportLabel, ...>` (the label union is
 * defined in `src/main.ts`), so this record is exhaustive by construction:
 * adding a new label in `src/main.ts` without a matching entry here is a
 * `typecheck:e2e` compile error, and vice versa. `desktop-1280x720` matches
 * `playwright.config.ts`'s default viewport, so scenarios that do not need
 * a non-default viewport can use it as a fixed, self-documenting label
 * instead of repeating raw numbers per spec file.
 */
export const VIEWPORT_LABELS: Readonly<Record<VisualScenarioViewportLabel, { width: number; height: number }>> = {
  'desktop-1280x720': { width: 1280, height: 720 },
}

// ---------------------------------------------------------------------------
// Visual baseline registry ids (Screenshot Target Guard Canvas exception)
// ---------------------------------------------------------------------------

/**
 * `id` column of `docs/dev/visual-baseline-registry.md`'s registry table
 * (Issue #1385 review, Blocker 3). `expectCanvasVisualCueScreenshot()`
 * requires a `registryId` drawn from this typed union — an ad hoc string
 * can no longer authorize a Canvas exception.
 */
export const VISUAL_BASELINE_REGISTRY_IDS = [
  'timeout-overlay',
  'running-hud',
  'defeat-overlay',
  'hp-label',
  'running-hud-paused',
  'result-overlay-timeout',
  'final-overlay-only',
  // Issue #1386 / PR #1721 review fix, P1 Blocker 2: a distinct id for this
  // spec's `[data-battle-ui-root]` full-overlay capture. Deliberately NOT
  // `running-hud` — the existing `running-hud` registry row already points
  // to `m2-combat-mvp.spec.ts`'s single `[data-field="sortie-status"]` field
  // baseline (118x66px, `maxDiffPixelRatio: 0.08` in that spec). Reusing the
  // same id for two unrelated baselines (different test file, different
  // capture root, different tolerance) would make the registry ambiguous.
  'running-hud-overlay-legacy-current',
] as const

export type VisualBaselineRegistryId = (typeof VISUAL_BASELINE_REGISTRY_IDS)[number]

/**
 * `maturity` column of `docs/dev/visual-baseline-registry.md`, mirrored here
 * (Issue #1386 PR #1721 review fix, P1 Blocker 2) so `expectDomOverlayScreenshot()`
 * can fail closed if a caller passes a `registryId` whose registry row is
 * `pending-baseline` (no committed PNG/test per the registry contract) —
 * without parsing the Markdown table at test time. Keep in sync with the
 * registry table by hand; a mismatch here is a docs/test drift bug, not a
 * runtime concern this module can fully own. `defeat-overlay` / `hp-label`
 * are `predicate-only` in the registry (not screenshot baselines), so they
 * are intentionally excluded from this screenshot-tolerant maturity union —
 * `expectDomOverlayScreenshot()` / `expectCanvasVisualCueScreenshot()` never
 * accept them as a screenshot `registryId` target.
 */
export const VISUAL_BASELINE_REGISTRY_MATURITY: Readonly<
  Record<
    Exclude<VisualBaselineRegistryId, 'defeat-overlay' | 'hp-label'>,
    'frozen' | 'provisional' | 'legacy-current' | 'pending-baseline'
  >
> = {
  'timeout-overlay': 'frozen',
  'running-hud': 'legacy-current',
  // Issue #1376 AC12: promoted 'pending-baseline' -> 'legacy-current' (not
  // 'frozen' -- frozen requires a merged PR SHA per
  // docs/dev/visual-baseline-registry.md's transition rules, deferred to
  // after #1377 per established precedent for the other overlay rows).
  'running-hud-paused': 'legacy-current',
  'result-overlay-timeout': 'legacy-current',
  'final-overlay-only': 'pending-baseline',
  'running-hud-overlay-legacy-current': 'legacy-current',
}

// ---------------------------------------------------------------------------
// Scenario installation (AC3, AC4)
// ---------------------------------------------------------------------------

/**
 * Binds an installed fixture to its `Page` (Issue #1385 review, Blocker 2)
 * so the screenshot helpers below can independently verify which scenario
 * (if any) is actually installed on a page, rather than trusting a
 * caller-supplied option.
 */
const installedScenarios = new WeakMap<Page, VisualScenarioFixture>()

/**
 * Installs a `window.__LOOP_VISUAL_SCENARIO__` fixture via
 * `page.addInitScript()`, and fixes the viewport to `fixture.viewportLabel`.
 * Must be called BEFORE `page.goto()` so the fixture is present at the
 * earliest point of app initialisation — before the app's first
 * `requestAnimationFrame` render (see src/main.ts). The app only honors
 * this fixture when built with `VITE_E2E_MODE=true`.
 *
 * Fails closed (Issue #1385 review, Blocker 2) for `pending-fixture`
 * scenarios (Scenario Support Matrix): a pending fixture's overlay surface
 * implementation child issue has not merged, so it must never be
 * installable — this is enforced here, not left to the caller of
 * `expectDomOverlayScreenshot()` to self-report correctly.
 */
export async function installVisualScenario(page: Page, fixture: VisualScenarioFixture): Promise<void> {
  if (isPendingFixtureScenario(fixture.name)) {
    throw new Error(
      `installVisualScenario(): scenario "${fixture.name}" is pending-fixture (Scenario Support ` +
        'Matrix) — its overlay surface implementation child issue has not merged, so it must not ' +
        'be installed (Issue #1385 review, Blocker 2).',
    )
  }
  await page.setViewportSize(VIEWPORT_LABELS[fixture.viewportLabel])
  await page.addInitScript((payload) => {
    ;(window as Window & { __LOOP_VISUAL_SCENARIO__?: unknown }).__LOOP_VISUAL_SCENARIO__ = payload
  }, fixture)
  installedScenarios.set(page, fixture)
}

// ---------------------------------------------------------------------------
// Screenshot target guard (AC2, AC5, Issue #1385 review Blocker 3)
// ---------------------------------------------------------------------------

/**
 * Locators rejected by default from `expectDomOverlayScreenshot()` —
 * full-canvas / full-shell bitmap captures (Screenshot Target Guard).
 * Evaluated against the resolved DOM element via `Element.matches()`, not
 * against `Locator.toString()` selector text, so compound selectors (e.g.
 * `page.locator('canvas.battle-stage__canvas')`) and `.describe()`
 * relabeling cannot bypass the guard (Issue #1385 review, Blocker 3).
 */
const FORBIDDEN_VISUAL_TARGET_SELECTOR = 'canvas, body, #app, .app-shell, .battle-stage'

interface ResolvedVisualTarget {
  tagName: string
  id: string
  classNames: string[]
  forbidden: boolean
}

async function resolveVisualTarget(locator: Locator): Promise<ResolvedVisualTarget> {
  return locator.evaluate(
    (element, selector) => ({
      tagName: element.tagName.toLowerCase(),
      id: element.id,
      classNames: Array.from(element.classList),
      forbidden: element.matches(selector),
    }),
    FORBIDDEN_VISUAL_TARGET_SELECTOR,
  )
}

/**
 * Asserts that `locator` resolves to an allowed default VRT screenshot
 * target (AC2, AC5, Screenshot Target Guard). Full-canvas / full-body /
 * full-shell bitmap captures are always rejected here — there is no
 * `allowCanvasVisualCue` escape hatch on this function (Issue #1385
 * review, Blocker 3): a deliberate Canvas visual-cue capture must use
 * `expectCanvasVisualCueScreenshot()` with a registry-tracked `registryId`
 * instead.
 */
export async function assertAllowedVisualTarget(locator: Locator): Promise<void> {
  const target = await resolveVisualTarget(locator)
  if (!target.forbidden) {
    return
  }
  const description = `<${target.tagName}${target.id ? `#${target.id}` : ''}${
    target.classNames.length ? `.${target.classNames.join('.')}` : ''
  }>`
  throw new Error(
    `assertAllowedVisualTarget(): resolved DOM target ${description} matches a rejected default ` +
      `screenshot target selector ("${FORBIDDEN_VISUAL_TARGET_SELECTOR}"). Full-canvas / ` +
      'full-body / full-shell bitmap screenshots are not promoted by this helper. Use ' +
      'expectCanvasVisualCueScreenshot() with a docs/dev/visual-baseline-registry.md registryId ' +
      'for an explicit, registry-tracked Canvas visual-cue exception.',
  )
}

// ---------------------------------------------------------------------------
// DOM overlay screenshot helper (AC2, AC5)
// ---------------------------------------------------------------------------

export interface DomOverlayScreenshotOptions {
  /**
   * `toHaveScreenshot()` absolute pixel-count tolerance. Mutually exclusive
   * with `maxDiffPixelRatio` (Playwright accepts only one). Prefer this over
   * `maxDiffPixelRatio` when the capture root includes a large masked
   * region (e.g. a `canvas` mask): `maxDiffPixelRatio` is computed against
   * the TOTAL pixel count including masked pixels, so a comparator budgeted
   * as a ratio of the full image silently allows a much larger absolute
   * pixel count to differ in the small non-masked (actually-compared)
   * region than the ratio number suggests (Issue #1386 PR #1721 review fix,
   * P1 Blocker 1).
   */
  maxDiffPixels?: number
  /** `toHaveScreenshot()` `maxDiffPixelRatio` (fraction of TOTAL pixels, mask included). */
  maxDiffPixelRatio?: number
  /**
   * Canvas exclusion strategy for this capture (Issue #1980). Defaults to
   * `'mask'` — the pre-existing Playwright `mask: [page.locator('canvas')]`
   * behavior, unchanged for every DOM overlay baseline that does not pass
   * this option. `'hidden'` instead sets `data-visual-canvas-hidden="true"`
   * on `<html>` before the screenshot (removed again in a `finally` block)
   * so `tests/e2e/visual.freeze.css`'s `[data-visual-canvas-hidden='true']
   * canvas { visibility: hidden !important; }` rule actually excludes the
   * `<canvas>` element from layout/paint, instead of Playwright drawing an
   * opaque mask rectangle OVER the canvas's bounding box. When the capture
   * root and the canvas share nearly the same bounding box (as
   * `running-hud-overlay-legacy-current` does since PR #1925,
   * `.battle-stage__viewport`), a `mask` rectangle covers the whole capture
   * root — including the HUD drawn on top of the canvas — so the baseline
   * silently stops verifying the HUD at all. `'hidden'` avoids that by
   * actually removing the canvas from the rendered output instead of
   * painting over the capture root.
   */
  canvasVisibility?: 'mask' | 'hidden'
}

/**
 * DOM overlay screenshot helper (AC2, AC5). Defaults reject full-canvas /
 * full-shell bitmap targets via `assertAllowedVisualTarget()` (resolved-DOM
 * based, Blocker 3). Applies the shared freeze CSS (AC1) via Playwright's
 * `stylePath` so animations, transitions, carets, and opted-in
 * volatile/mask elements are frozen, and masks every `canvas` element
 * (Issue #1385 review, Blocker 4) so a transparent DOM overlay capture
 * never includes the Canvas battle-stage bitmap rendered behind it.
 *
 * Requires a fixture to have been bound to `locator.page()` via
 * `installVisualScenario()` first (Blocker 2) — the scenario is read from
 * that binding, not from a caller-supplied option, so a caller cannot
 * self-report a scenario that was never actually installed. Pending-fixture
 * scenarios (`VISUAL_SCENARIO_STATUS`) fail closed (Scenario Support
 * Matrix): this helper refuses to capture a baseline for a scenario whose
 * implementation surface has not merged, so the current pre-overlay UI is
 * never accidentally frozen as the expected baseline.
 *
 * `registryId` (Issue #1386 PR #1721 review fix, P1 Blocker 2) is required
 * and must name an ACTIVE (non-`pending-baseline`) row in
 * `VISUAL_BASELINE_REGISTRY_MATURITY` / `docs/dev/visual-baseline-registry.md`
 * — an ad hoc string can no longer authorize a DOM overlay baseline capture,
 * mirroring `expectCanvasVisualCueScreenshot()`'s existing `registryId`
 * contract.
 *
 * `options` no longer defaults `maxDiffPixelRatio` to a shared 0.02 value
 * (Issue #1386 PR #1721 review fix, P1 Blocker 1): the previous shared
 * default was computed against the FULL capture (including the large
 * `canvas` mask this helper always applies), so it silently tolerated a
 * much larger fraction of the actually-compared non-masked pixels than
 * 0.02 suggests, and was loose enough to pass through a real text-reflow
 * layout regression. Every call site must now pass an explicit
 * `maxDiffPixels` or `maxDiffPixelRatio`; this throws if neither is given.
 */
export async function expectDomOverlayScreenshot(
  locator: Locator,
  name: string,
  registryId: VisualBaselineRegistryId,
  options: DomOverlayScreenshotOptions,
): Promise<void> {
  const page = locator.page()
  const installed = installedScenarios.get(page)
  if (!installed) {
    throw new Error(
      'expectDomOverlayScreenshot(): no visual scenario is bound to this page. Call ' +
        'installVisualScenario(page, fixture) before page.goto().',
    )
  }
  if (isPendingFixtureScenario(installed.name)) {
    throw new Error(
      `expectDomOverlayScreenshot(): scenario "${installed.name}" is pending-fixture ` +
        '(Scenario Support Matrix). Active screenshot helpers must not capture a frozen ' +
        'baseline for a surface whose implementation child issue has not merged.',
    )
  }
  if (!(registryId in VISUAL_BASELINE_REGISTRY_MATURITY)) {
    throw new Error(
      `expectDomOverlayScreenshot(): registryId "${String(registryId)}" has no maturity entry in ` +
        'VISUAL_BASELINE_REGISTRY_MATURITY / docs/dev/visual-baseline-registry.md.',
    )
  }
  const maturity = (VISUAL_BASELINE_REGISTRY_MATURITY as Record<string, string>)[registryId]
  if (maturity === 'pending-baseline') {
    throw new Error(
      `expectDomOverlayScreenshot(): registryId "${registryId}" is pending-baseline in ` +
        'docs/dev/visual-baseline-registry.md (no committed PNG/test per the registry contract) — ' +
        'it must not be captured yet.',
    )
  }
  if (options.maxDiffPixels === undefined && options.maxDiffPixelRatio === undefined) {
    throw new Error(
      'expectDomOverlayScreenshot(): an explicit maxDiffPixels or maxDiffPixelRatio is required ' +
        '(no shared default — Issue #1386 PR #1721 review fix, P1 Blocker 1). Measure the tolerance ' +
        'against this capture root empirically rather than reusing another registry entry\'s value.',
    )
  }

  await assertAllowedVisualTarget(locator)

  const diffToleranceOption =
    options.maxDiffPixels !== undefined
      ? { maxDiffPixels: options.maxDiffPixels }
      : { maxDiffPixelRatio: options.maxDiffPixelRatio }

  const canvasVisibility = options.canvasVisibility ?? 'mask'

  if (canvasVisibility === 'mask') {
    // Fail-closed geometric guard (Issue #1980 review fix, P1 Blocker 2):
    // this Issue's root cause was a `mask` rectangle silently painting over
    // the ENTIRE capture root (including the HUD drawn on top of the
    // canvas) whenever the capture root and the canvas share nearly the
    // same bounding box -- the resulting screenshot assertion still passed,
    // but no longer verified anything. Rather than rely on every future
    // call site remembering to pass `canvasVisibility: 'hidden'` for a
    // capture root shaped like this, detect the dangerous geometry here and
    // throw instead of silently producing a no-op capture. The DEFAULT
    // behavior (`'mask'`) is unchanged for every capture root where the
    // canvas does NOT cover most of the root's area -- this only adds a new
    // throw path for the specific case that caused this Issue, and does not
    // run at all for the `'hidden'` branch below (so the existing
    // `running-hud-overlay-legacy-current` call site, which already passes
    // `canvasVisibility: 'hidden'`, never reaches this check).
    const canvasLocator = page.locator('canvas')
    const canvasCount = await canvasLocator.count()
    if (canvasCount > 0) {
      const [rootBox, canvasBox] = await Promise.all([locator.boundingBox(), canvasLocator.first().boundingBox()])
      if (rootBox && canvasBox && rootBox.width > 0 && rootBox.height > 0) {
        const rootArea = rootBox.width * rootBox.height
        const intersectionLeft = Math.max(rootBox.x, canvasBox.x)
        const intersectionTop = Math.max(rootBox.y, canvasBox.y)
        const intersectionRight = Math.min(rootBox.x + rootBox.width, canvasBox.x + canvasBox.width)
        const intersectionBottom = Math.min(rootBox.y + rootBox.height, canvasBox.y + canvasBox.height)
        const intersectionWidth = Math.max(0, intersectionRight - intersectionLeft)
        const intersectionHeight = Math.max(0, intersectionBottom - intersectionTop)
        const intersectionArea = intersectionWidth * intersectionHeight
        // Threshold chosen to match the geometry that actually caused this
        // Issue (canvas and capture root sharing ~the same bounding box
        // since PR #1925's `.battle-stage__viewport`) while leaving margin
        // for legitimate capture roots where a canvas only partially
        // overlaps (e.g. a small inline canvas icon inside a larger DOM
        // overlay), which should keep using the default `mask` behavior.
        const CANVAS_MASK_COVERAGE_GUARD_THRESHOLD = 0.9
        const coverageRatio = intersectionArea / rootArea
        if (coverageRatio > CANVAS_MASK_COVERAGE_GUARD_THRESHOLD) {
          throw new Error(
            "expectDomOverlayScreenshot(): canvasVisibility 'mask' (default) would cover " +
              `${(coverageRatio * 100).toFixed(1)}% of the capture root's area with an opaque mask ` +
              "rectangle -- this reproduces Issue #1980's root cause (a mask painting over the " +
              'content under test, e.g. the HUD drawn on top of the canvas). Pass ' +
              "canvasVisibility: 'hidden' instead so the canvas element is excluded via CSS " +
              'visibility rather than masked over.',
          )
        }
      }
    }
  }

  if (canvasVisibility === 'hidden') {
    // Issue #1980: exclude the canvas via CSS visibility (freeze CSS opt-in
    // flag) instead of a Playwright `mask` rectangle, so a capture root that
    // shares nearly the same bounding box as the canvas (e.g.
    // `running-hud-overlay-legacy-current`) does not get its whole area
    // painted over — the HUD drawn on top of the canvas stays capturable.
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-visual-canvas-hidden', 'true')
    })
    try {
      await expect(locator).toHaveScreenshot(name, {
        animations: 'disabled',
        ...diffToleranceOption,
        stylePath: VISUAL_FREEZE_CSS_PATH,
      })
    } finally {
      await page.evaluate(() => {
        document.documentElement.removeAttribute('data-visual-canvas-hidden')
      })
    }
    return
  }

  await expect(locator).toHaveScreenshot(name, {
    animations: 'disabled',
    ...diffToleranceOption,
    stylePath: VISUAL_FREEZE_CSS_PATH,
    // BLOCKER 4 (Issue #1385 review): mask every canvas so a transparent
    // DOM overlay (e.g. `.battle-ui-layer`, `position: absolute; inset: 0`)
    // never captures the Canvas bitmap rendered behind it. Only used when
    // `canvasVisibility` is `'mask'` (default) — see the `'hidden'` branch
    // above for `running-hud-overlay-legacy-current` (Issue #1980).
    mask: [page.locator('canvas')],
  })
}

// ---------------------------------------------------------------------------
// Canvas visual cue screenshot helper (Issue #1385 review, Blocker 3)
// ---------------------------------------------------------------------------

export interface CanvasVisualCueScreenshotOptions {
  maxDiffPixels?: number
}

/**
 * Explicit, registry-tracked Canvas visual-cue screenshot helper. Separate
 * from `expectDomOverlayScreenshot()` (Issue #1385 review, Blocker 3): the
 * normal DOM overlay helper never allows a Canvas target, so a caller
 * cannot bypass the Screenshot Target Guard by passing exception options.
 * Requires a `registryId` present in `VISUAL_BASELINE_REGISTRY_IDS`
 * (mirrors `docs/dev/visual-baseline-registry.md`'s `id` column) as the
 * audit trail that this capture is a deliberate, registry-tracked
 * exception. Still requires an installed, non-pending scenario (same
 * binding + fail-closed rules as `expectDomOverlayScreenshot()`), and still
 * applies the shared freeze CSS — but does NOT mask canvases.
 *
 * `options.maxDiffPixels` is REQUIRED for a REAL capture (PR #1813 review
 * fix, P2 Blocker 6): the previous shared `maxDiffPixels ?? 1` runtime
 * default silently diverged from `scripts/check-visual-artifact-pipeline.py`,
 * which treats a call site with no comparator option as a hard-fail contract
 * violation ("neither maxDiffPixels nor maxDiffPixelRatio declared"). A
 * future caller that omitted `options` would pass at runtime while failing
 * CI's static validator. This check runs AFTER the Canvas-target guard
 * below (not before) so existing negative/guard tests that intentionally
 * omit `options` while asserting a DIFFERENT rejection reason (a non-Canvas
 * target, an unknown registryId) keep failing for that reason, not this one
 * — `options` itself stays an optional parameter (default `{}`) for that
 * reason; only an actual Canvas-target, valid-registryId call requires an
 * explicit `maxDiffPixels`.
 */
export async function expectCanvasVisualCueScreenshot(
  locator: Locator,
  name: string,
  registryId: VisualBaselineRegistryId,
  options: CanvasVisualCueScreenshotOptions = {},
): Promise<void> {
  const page = locator.page()
  const installed = installedScenarios.get(page)
  if (!installed) {
    throw new Error(
      'expectCanvasVisualCueScreenshot(): no visual scenario is bound to this page. Call ' +
        'installVisualScenario(page, fixture) before page.goto().',
    )
  }
  if (isPendingFixtureScenario(installed.name)) {
    throw new Error(
      `expectCanvasVisualCueScreenshot(): scenario "${installed.name}" is pending-fixture ` +
        '(Scenario Support Matrix). Active screenshot helpers must not capture a frozen ' +
        'baseline for a surface whose implementation child issue has not merged.',
    )
  }
  if (!VISUAL_BASELINE_REGISTRY_IDS.includes(registryId)) {
    throw new Error(
      `expectCanvasVisualCueScreenshot(): unknown registryId "${String(registryId)}" — must be a ` +
        'docs/dev/visual-baseline-registry.md registry id.',
    )
  }
  // This helper exists ONLY to capture an actual <canvas> element's visual
  // cue — it must not become a second route to a full-body/#app/.app-shell/
  // .battle-stage bitmap capture (Issue #1385 review, Blocker 3). Resolved
  // against the DOM element's tag name, not selector text, for the same
  // reason as `assertAllowedVisualTarget()`.
  const target = await resolveVisualTarget(locator)
  if (target.tagName !== 'canvas') {
    const description = `<${target.tagName}${target.id ? `#${target.id}` : ''}${
      target.classNames.length ? `.${target.classNames.join('.')}` : ''
    }>`
    throw new Error(
      `expectCanvasVisualCueScreenshot(): resolved DOM target ${description} is not a <canvas> ` +
        'element. This helper only captures an actual Canvas visual cue; ' +
        'body/#app/.app-shell/.battle-stage full-shell captures are never allowed, including via ' +
        'this exception helper.',
    )
  }
  if (options.maxDiffPixels === undefined) {
    throw new Error(
      'expectCanvasVisualCueScreenshot(): an explicit maxDiffPixels is required (no shared ' +
        'default — PR #1813 review fix, P2 Blocker 6). Measure the tolerance against this ' +
        'capture root empirically rather than relying on a previous implicit default.',
    )
  }

  await expect(locator).toHaveScreenshot(name, {
    animations: 'disabled',
    maxDiffPixels: options.maxDiffPixels,
    stylePath: VISUAL_FREEZE_CSS_PATH,
  })
}
