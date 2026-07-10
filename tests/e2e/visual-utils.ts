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
  'final-no-command-rail',
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
  'running-hud-paused': 'pending-fixture',
  'result-timeout': 'pending-fixture',
  'final-no-command-rail': 'pending-fixture',
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
  'final-no-command-rail',
] as const

export type VisualBaselineRegistryId = (typeof VISUAL_BASELINE_REGISTRY_IDS)[number]

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
  maxDiffPixels?: number
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
 */
export async function expectDomOverlayScreenshot(
  locator: Locator,
  name: string,
  options: DomOverlayScreenshotOptions = {},
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

  await assertAllowedVisualTarget(locator)

  await expect(locator).toHaveScreenshot(name, {
    animations: 'disabled',
    maxDiffPixels: options.maxDiffPixels ?? 1,
    stylePath: VISUAL_FREEZE_CSS_PATH,
    // BLOCKER 4 (Issue #1385 review): mask every canvas so a transparent
    // DOM overlay (e.g. `.battle-ui-layer`, `position: absolute; inset: 0`)
    // never captures the Canvas bitmap rendered behind it.
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

  await expect(locator).toHaveScreenshot(name, {
    animations: 'disabled',
    maxDiffPixels: options.maxDiffPixels ?? 1,
    stylePath: VISUAL_FREEZE_CSS_PATH,
  })
}
