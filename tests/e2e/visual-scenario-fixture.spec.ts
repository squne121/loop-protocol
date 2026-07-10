/**
 * E2E: VRT visual scenario fixture + Screenshot Target Guard (Issue #1385
 * human review REQUEST_CHANGES fix_delta).
 *
 * Runs in the existing CI `e2e` job (Playwright, `tests/e2e/**`), NOT
 * `pnpm test` (vitest excludes `tests/e2e/**`) — this file (and
 * `tests/e2e/visual-utils.ts`) are only type-checked by `pnpm typecheck:e2e`
 * and only behaviorally exercised here.
 *
 * These specs deliberately avoid `expectDomOverlayScreenshot()` /
 * `expectCanvasVisualCueScreenshot()` happy-path (would-be-successful)
 * calls: those end in `toHaveScreenshot()`, which requires a committed
 * baseline PNG under `tests/e2e/__screenshots__/**` — out of this Issue's
 * Allowed Paths. Every guard behavior below is provable by asserting a
 * **rejection** (the guard throws before `toHaveScreenshot()` is ever
 * reached), or via predicate-only checks (no baseline PNG), matching this
 * repo's `predicate-only` registry kind (`docs/dev/visual-baseline-registry.md`).
 */

import { test, expect, type Page } from '@playwright/test'
import {
  installVisualScenario,
  expectDomOverlayScreenshot,
  expectCanvasVisualCueScreenshot,
  type VisualScenarioFixture,
} from './visual-utils'

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const RUNNING_FIXTURE: VisualScenarioFixture = {
  name: 'running-hud',
  loopPhase: 'running',
  paused: false,
  sortie: { status: 'running', elapsedTicks: 900, fixedDeltaMs: 16 },
  player: { hp: 80, maxHp: 100 },
  progress: { resources: 12, weaponPower: 1 },
  telemetry: { status: '', summary: '' },
  viewportLabel: 'desktop-1280x720',
}

/** Mirrors the read-only `__LOOP_E2E__` hook in `src/main.ts` (AC12). */
interface LoopE2EState {
  loopPhase: string
  sortie: { status: string; elapsedTicks: number }
}

async function getGameState(page: Page): Promise<LoopE2EState> {
  return page.evaluate(() => {
    const hook = (window as Window & { __LOOP_E2E__?: { getState: () => LoopE2EState } })
      .__LOOP_E2E__
    if (!hook) {
      throw new Error('__LOOP_E2E__ hook not found. Was the app built with VITE_E2E_MODE=true?')
    }
    return hook.getState()
  })
}

async function getVisualScenarioHookPresence(page: Page): Promise<boolean> {
  return page.evaluate(() => Boolean((window as Window & { __LOOP_E2E__?: unknown }).__LOOP_E2E__))
}

// ---------------------------------------------------------------------------
// Blocker 1: running fixture must not self-transition to timeout/result
// ---------------------------------------------------------------------------

test('GIVEN a running-hud visual scenario fixture WHEN several RAF frames and wall-clock time elapse THEN loopPhase/sortie.status/elapsedTicks stay frozen', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_FIXTURE)
  await page.goto('/')

  const initial = await getGameState(page)
  expect(initial.loopPhase).toBe('running')
  expect(initial.sortie.status).toBe('running')
  expect(initial.sortie.elapsedTicks).toBe(RUNNING_FIXTURE.sortie.elapsedTicks)

  // Advance several real RAF frames.
  for (let i = 0; i < 5; i += 1) {
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)))
  }

  // Also let real wall-clock time pass (well beyond one fixed simulation step).
  await page.waitForTimeout(500)

  const after = await getGameState(page)
  expect(after.loopPhase).toBe('running')
  expect(after.sortie.status).toBe('running')
  expect(after.sortie.elapsedTicks).toBe(RUNNING_FIXTURE.sortie.elapsedTicks)
})

// ---------------------------------------------------------------------------
// Blocker 2: pending-fixture / no-binding fail-closed guard
// ---------------------------------------------------------------------------

test('GIVEN no visual scenario installed on the page WHEN expectDomOverlayScreenshot is called THEN it rejects (scenario omitted cannot be captured)', async ({
  page,
}) => {
  await page.goto('/')
  const overlay = page.locator('[data-battle-ui-root]')

  await expect(expectDomOverlayScreenshot(overlay, 'no-scenario-bound')).rejects.toThrow(
    /no visual scenario is bound to this page/,
  )
})

test('GIVEN a fixture payload shaped for a pending scenario relabeled with an active scenario name WHEN the app boots THEN it is rejected before any capture is possible (relabeling cannot fabricate an active binding)', async ({
  page,
}) => {
  // Attack: bypass installVisualScenario()'s own name-based pending check by
  // injecting a raw window payload whose declared `name` is the ACTIVE
  // 'running-hud', but whose `sortie` shape is the TERMINAL shape only
  // valid for the (pending) result-phase scenarios. src/main.ts's runtime
  // validator (parseVisualScenarioFixture) independently rejects any
  // name/loopPhase/sortie-status mismatch (Issue #1385 review, additional
  // 指摘7), so a relabeled payload can never reach `applyVisualScenarioFixture()`.
  await page.addInitScript(() => {
    // Cast through `unknown` first (not a direct intersection with
    // `Window`): `tests/e2e/window.d.ts`'s ambient `Window.__LOOP_VISUAL_SCENARIO__?:
    // VisualScenarioFixture` global augmentation is in scope for this whole
    // `typecheck:e2e` program, and `T & unknown` collapses back to `T`, so an
    // `as Window & { __LOOP_VISUAL_SCENARIO__?: unknown }` intersection would
    // NOT actually widen the property — it would still be checked against
    // `VisualScenarioFixture`, defeating the deliberately-invalid payload
    // below.
    ;(window as unknown as { __LOOP_VISUAL_SCENARIO__?: unknown }).__LOOP_VISUAL_SCENARIO__ = {
      name: 'running-hud',
      loopPhase: 'result',
      paused: false,
      sortie: { status: 'timeout', elapsedTicks: 900, fixedDeltaMs: 16, durationMs: 14400, kills: 3 },
      player: { hp: 80, maxHp: 100 },
      progress: { resources: 12, weaponPower: 1 },
      telemetry: { status: '', summary: '' },
      viewportLabel: 'desktop-1280x720',
    }
  })
  // NOTE: installVisualScenario() is deliberately NOT called — this
  // simulates an attempt to bind a scenario outside the guarded install
  // path. No installedScenarios entry is ever created for this page.
  await page.goto('/')

  // src/main.ts's runtime validator rejects the mismatched payload and
  // throws during module top-level evaluation, so the __LOOP_E2E__
  // observability hook (registered further down that same module) never
  // gets installed — the app fails closed rather than fabricating a
  // usable running-hud state from a relabeled pending payload.
  expect(await getVisualScenarioHookPresence(page)).toBe(false)

  // Even if a caller tried to capture anyway, there is no installedScenarios
  // binding for this page (installVisualScenario() was never called), so
  // the screenshot helper independently refuses to capture.
  const overlay = page.locator('[data-battle-ui-root]')
  await expect(expectDomOverlayScreenshot(overlay, 'relabeled-pending')).rejects.toThrow(
    /no visual scenario is bound to this page/,
  )
})

// ---------------------------------------------------------------------------
// Blocker 3: resolved-DOM Screenshot Target Guard
// ---------------------------------------------------------------------------

test('GIVEN a compound selector locator for the Canvas WHEN expectDomOverlayScreenshot is called THEN it is rejected (compound selectors cannot bypass the guard)', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_FIXTURE)
  await page.goto('/')

  const compoundCanvasLocator = page.locator('canvas.battle-stage__canvas')
  await expect(expectDomOverlayScreenshot(compoundCanvasLocator, 'compound-canvas')).rejects.toThrow(
    /rejected default screenshot target/,
  )
})

test('GIVEN a Canvas locator disguised via .describe() WHEN expectDomOverlayScreenshot is called THEN it is still rejected (guard resolves the actual DOM element, not selector text)', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_FIXTURE)
  await page.goto('/')

  const disguisedCanvasLocator = page.locator('canvas').describe('totally-not-a-canvas')
  await expect(expectDomOverlayScreenshot(disguisedCanvasLocator, 'describe-disguised-canvas')).rejects.toThrow(
    /rejected default screenshot target/,
  )
})

test('GIVEN the <body> element WHEN expectDomOverlayScreenshot OR expectCanvasVisualCueScreenshot is called THEN both are rejected (no Canvas exception ever applies to a full-shell target)', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_FIXTURE)
  await page.goto('/')

  const bodyLocator = page.locator('body')
  await expect(expectDomOverlayScreenshot(bodyLocator, 'body-target')).rejects.toThrow(
    /rejected default screenshot target/,
  )
  await expect(
    expectCanvasVisualCueScreenshot(bodyLocator, 'body-target-canvas-cue', 'running-hud'),
  ).rejects.toThrow(/is not a <canvas> element/)
})

test('GIVEN an unknown registryId WHEN expectCanvasVisualCueScreenshot is called on the actual Canvas THEN it is rejected', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_FIXTURE)
  await page.goto('/')

  const canvasLocator = page.locator('canvas')
  await expect(
    expectCanvasVisualCueScreenshot(
      canvasLocator,
      'unknown-registry-id',
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- deliberately-invalid registryId for the fail-closed assertion below
      'not-a-real-registry-entry' as any,
    ),
  ).rejects.toThrow(/unknown registryId/)
})

// ---------------------------------------------------------------------------
// Additional指摘7/8: runtime-validated viewportLabel
// ---------------------------------------------------------------------------

test('GIVEN a fixture payload with an unknown viewportLabel WHEN the app boots THEN it is rejected (fails closed rather than silently using a default viewport)', async ({
  page,
}) => {
  await page.addInitScript(() => {
    // Cast through `unknown` first (not a direct intersection with
    // `Window`): `tests/e2e/window.d.ts`'s ambient `Window.__LOOP_VISUAL_SCENARIO__?:
    // VisualScenarioFixture` global augmentation is in scope for this whole
    // `typecheck:e2e` program, and `T & unknown` collapses back to `T`, so an
    // `as Window & { __LOOP_VISUAL_SCENARIO__?: unknown }` intersection would
    // NOT actually widen the property — it would still be checked against
    // `VisualScenarioFixture`, defeating the deliberately-invalid payload
    // below.
    ;(window as unknown as { __LOOP_VISUAL_SCENARIO__?: unknown }).__LOOP_VISUAL_SCENARIO__ = {
      name: 'running-hud',
      loopPhase: 'running',
      paused: false,
      sortie: { status: 'running', elapsedTicks: 900, fixedDeltaMs: 16 },
      player: { hp: 80, maxHp: 100 },
      progress: { resources: 12, weaponPower: 1 },
      telemetry: { status: '', summary: '' },
      viewportLabel: 'not-a-real-viewport-label',
    }
  })
  await page.goto('/')

  // src/main.ts's runtime validator rejects the unknown viewportLabel and
  // throws during module top-level evaluation, so the __LOOP_E2E__
  // observability hook (registered further down that same module) never
  // gets installed.
  expect(await getVisualScenarioHookPresence(page)).toBe(false)
})

// ---------------------------------------------------------------------------
// Blocker 4: DOM overlay captures must exclude Canvas pixels
// ---------------------------------------------------------------------------

test('GIVEN a DOM overlay positioned over the Canvas WHEN captured with the same canvas mask expectDomOverlayScreenshot() applies THEN the masked capture differs from an unmasked capture (Canvas pixels are excluded)', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_FIXTURE)
  await page.goto('/')

  // `.battle-ui-layer[data-battle-ui-root]` is `position: absolute; inset: 0`
  // directly over `.battle-stage__canvas` (src/main.ts app shell markup) —
  // exactly the transparent-DOM-overlay-over-Canvas case Blocker 4 covers.
  const overlay = page.locator('[data-battle-ui-root]')
  const canvasLocator = page.locator('canvas')

  // Uses Locator.screenshot() directly (not expectDomOverlayScreenshot()/
  // toHaveScreenshot()) so this predicate-only check needs no committed
  // baseline PNG — it exercises the exact same `mask` mechanism
  // `expectDomOverlayScreenshot()` applies to every canvas.
  const maskedBuffer = await overlay.screenshot({ mask: [canvasLocator], animations: 'disabled' })
  const unmaskedBuffer = await overlay.screenshot({ animations: 'disabled' })

  expect(maskedBuffer.equals(unmaskedBuffer)).toBe(false)
})
