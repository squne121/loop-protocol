/**
 * visual-utils.ts — VRT fixture and screenshot-target-guard helpers (Issue #1385).
 *
 * This module is a Playwright E2E helper (imports `@playwright/test` types
 * directly) and is deliberately test-only: it is never imported from `src/`.
 * The scenario name/type constants below mirror (but are not imported by)
 * the fixture-reading type declared inline in `src/main.ts`, so production
 * code never depends on this test module.
 */

import { expect, type Locator, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'

// ---------------------------------------------------------------------------
// Scenario Support Matrix (Issue #1385 contract)
// ---------------------------------------------------------------------------

export const VISUAL_SCENARIOS = [
  'running-hud',
  'running-hud-paused',
  'result-timeout',
  'final-no-command-rail',
] as const

export type VisualScenarioName = (typeof VISUAL_SCENARIOS)[number]

export type VisualScenarioFixtureStatus = 'active-fixture-only' | 'pending-fixture'

/**
 * `active-fixture-only`: the scenario's fixture data may be installed and
 * captured by `expectDomOverlayScreenshot()`.
 * `pending-fixture`: the scenario name is reserved only. Its overlay
 * surface implementation child issue has not merged yet
 * (`docs/dev/visual-baseline-registry.md`). `expectDomOverlayScreenshot()`
 * fails closed for these scenarios rather than silently freezing the
 * current pre-overlay UI (e.g. the legacy `.command-rail`) as a baseline.
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
// Fixture shape (AC3)
// ---------------------------------------------------------------------------

/**
 * Deterministic visual scenario fixture, installed as
 * `window.__LOOP_VISUAL_SCENARIO__` before the app's first render (see
 * `installVisualScenario()` below and `src/main.ts`). Fixes phase, sortie
 * state, pause state, resources, upgrades (`weaponPower`), duration, kills,
 * hull (player hp/maxHp), viewport label, and transient copy
 * (`telemetry`) so VRT captures are deterministic (AC3).
 */
export interface VisualScenarioFixture {
  name: VisualScenarioName
  loopPhase: 'preparation' | 'running' | 'result'
  paused: boolean
  sortie: {
    status: 'running' | 'timeout'
    elapsedTicks: number
    fixedDeltaMs: number
    /** Terminal duration authority (Timer / Volatile Text Policy). Required when status is 'timeout'. */
    durationMs?: number
    kills: number
  }
  player: { hp: number; maxHp: number }
  progress: { resources: number; weaponPower: number }
  /** Transient HUD copy (status line / last command summary). */
  telemetry: { status: string; summary: string }
  /** Fixed label describing the intended capture viewport (see VIEWPORT_LABELS below). */
  viewportLabel: string
  /**
   * Opt-in visual masks applied via `data-visual-mask="true"` (see
   * `visual.freeze.css`). `duration: true` masks the running-duration field
   * until its display authority is `elapsedTicks * fixedDeltaMs` (Timer /
   * Volatile Text Policy). Reserved for scenario/test authors; the running
   * HUD's own duration field self-masks in `HudController` (Issue #1385).
   */
  visualMask?: { duration?: boolean; evidencePanel?: boolean }
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
 * `desktop-1280x720` matches `playwright.config.ts`'s default viewport, so
 * scenarios that do not need a non-default viewport can use it as a fixed,
 * self-documenting label instead of repeating raw numbers per spec file.
 */
export const VIEWPORT_LABELS: Readonly<Record<string, { width: number; height: number }>> = {
  'desktop-1280x720': { width: 1280, height: 720 },
}

// ---------------------------------------------------------------------------
// Scenario installation (AC3, AC4)
// ---------------------------------------------------------------------------

/**
 * Installs a `window.__LOOP_VISUAL_SCENARIO__` fixture via
 * `page.addInitScript()`, and fixes the viewport when `fixture.viewportLabel`
 * matches a known `VIEWPORT_LABELS` entry. Must be called BEFORE
 * `page.goto()` so the fixture is present at the earliest point of app
 * initialisation — before the app's first `requestAnimationFrame` render
 * (see src/main.ts). The app only honors this fixture when built with
 * `VITE_E2E_MODE=true`.
 */
export async function installVisualScenario(page: Page, fixture: VisualScenarioFixture): Promise<void> {
  const viewport = VIEWPORT_LABELS[fixture.viewportLabel]
  if (viewport) {
    await page.setViewportSize(viewport)
  }
  await page.addInitScript((payload) => {
    ;(window as Window & { __LOOP_VISUAL_SCENARIO__?: unknown }).__LOOP_VISUAL_SCENARIO__ = payload
  }, fixture)
}

// ---------------------------------------------------------------------------
// Screenshot target guard (AC2, AC5)
// ---------------------------------------------------------------------------

/** Locators rejected by default — full-canvas / full-shell bitmap captures (Screenshot Target Guard). */
const REJECTED_DEFAULT_SELECTORS = ['canvas', 'body', '#app', '.app-shell', '.battle-stage'] as const

export interface DomOverlayScreenshotOptions {
  maxDiffPixels?: number
  /** Correlates this capture to a scenario for the pending-fixture fail-closed guard. */
  scenario?: VisualScenarioName
  /**
   * Explicit opt-in to capture a Canvas visual cue. Requires `registryId`
   * to also be set — the pairing is the audit trail that this capture was
   * a deliberate, registry-tracked exception (AC5, Screenshot Target Guard).
   */
  allowCanvasVisualCue?: boolean
  /** `docs/dev/visual-baseline-registry.md` entry id authorizing the Canvas exception. */
  registryId?: string
}

function isRejectedDefaultTarget(locator: Locator): boolean {
  // Locator.toString() surfaces the underlying selector for diagnostics
  // (e.g. "locator('canvas')"); it is the only public way to introspect a
  // Locator's selector in Playwright's synchronous API.
  const description = locator.toString()
  return REJECTED_DEFAULT_SELECTORS.some((selector) => description.includes(`locator('${selector}')`))
}

/**
 * Asserts that `locator` is an allowed default VRT screenshot target
 * (AC2, AC5, Screenshot Target Guard). Full-canvas / full-body / full-shell
 * bitmap captures are rejected unless the caller explicitly opts in via
 * `allowCanvasVisualCue: true` AND supplies a `registryId`.
 */
export function assertAllowedVisualTarget(
  locator: Locator,
  options: Pick<DomOverlayScreenshotOptions, 'allowCanvasVisualCue' | 'registryId'> = {},
): void {
  if (!isRejectedDefaultTarget(locator)) {
    return
  }
  if (options.allowCanvasVisualCue === true && options.registryId) {
    return
  }
  throw new Error(
    `assertAllowedVisualTarget(): target "${locator.toString()}" is a rejected default ` +
      'screenshot target (canvas/body/#app/.app-shell/.battle-stage). Full-canvas / ' +
      'full-shell bitmap screenshots are not promoted by this helper. Pass ' +
      '{ allowCanvasVisualCue: true, registryId: "<visual-baseline-registry.md id>" } ' +
      'for an explicit, registry-tracked Canvas visual-cue exception.',
  )
}

/**
 * DOM overlay screenshot helper (AC2, AC5). Defaults reject full-canvas /
 * full-shell bitmap targets (`assertAllowedVisualTarget`). Applies the
 * shared freeze CSS (AC1) via Playwright's `stylePath` so animations,
 * transitions, carets, and opted-in volatile/mask elements are frozen.
 *
 * Pending-fixture scenarios (`VISUAL_SCENARIO_STATUS`) fail closed
 * (Scenario Support Matrix): this helper refuses to capture a baseline for
 * a scenario whose implementation surface has not merged, so the current
 * pre-overlay UI is never accidentally frozen as the expected baseline.
 */
export async function expectDomOverlayScreenshot(
  locator: Locator,
  name: string,
  options: DomOverlayScreenshotOptions = {},
): Promise<void> {
  if (options.scenario && isPendingFixtureScenario(options.scenario)) {
    throw new Error(
      `expectDomOverlayScreenshot(): scenario "${options.scenario}" is pending-fixture ` +
        '(Scenario Support Matrix). Active screenshot helpers must not capture a frozen ' +
        'baseline for a surface whose implementation child issue has not merged.',
    )
  }

  assertAllowedVisualTarget(locator, options)

  await expect(locator).toHaveScreenshot(name, {
    animations: 'disabled',
    maxDiffPixels: options.maxDiffPixels ?? 1,
    stylePath: VISUAL_FREEZE_CSS_PATH,
  })
}
