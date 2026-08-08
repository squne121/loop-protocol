import { afterEach, beforeEach, expect, test } from 'vitest'
import { page } from 'vitest/browser'
import { createHudController, type HudActions } from '../../src/ui/HudController'
import { createInitialGameState } from '../../src/state/GameState'
import '../../src/style.css'
// PR #1977 review fix (P1 item 5, OWNER REQUEST_CHANGES): reuse the
// existing shared freeze CSS (Issue #1385) that
// `tests/e2e/visual-overlay.spec.ts` already applies via Playwright's
// `stylePath` option (Issue #1389's own In Scope text: "既存 freeze CSS
// 再利用"). Read-only import from `tests/component/**` — this file itself
// is NOT modified (it is outside this Issue's Allowed Paths). Pins
// `[data-battle-ui-root]` (present on this test's mount `container` below)
// to a generic `sans-serif` font stack and disables
// animation/transition/caret/smooth-scroll, removing the named-font
// fallback-chain drift that `src/style.css` alone leaves host-dependent —
// the same class of determinism gap the CI-environment baseline
// regeneration (Issue #1389 fix_delta, PR #1977 prior iteration) already
// had to work around for the Canvas/Chromium rendering side.
import '../e2e/visual.freeze.css'

/**
 * tests/component/combat-hud-running.vrt.test.ts — Vitest Browser Mode
 * component VRT scaffold (Issue #1389, `#1380` VRT rollout tracker),
 * re-scoped by Issue #2014 (comparator sensitivity / critical subregion /
 * mutation negative control / evidence retrofit — PR #2024 OWNER
 * REQUEST_CHANGES).
 *
 * Report-only lane: backs the non-required `component-vrt-report` CI job
 * only (In Scope / Out of Scope). `combat-hud-running` (running) and
 * `combat-hud-critical` (hp=25/maxHp=100, Issue #2014 AC4) are the ONLY
 * scenarios in this Issue's Current Validated Scope — result / pause modal
 * / final-no-command-rail are explicitly excluded until their UI stabilizes
 * in a later Issue.
 *
 * Mounts production DOM via `createHudController()`
 * (`src/ui/HudController.ts`) with production `src/style.css`, and
 * captures ONLY the `[data-combat-hud]` surface (`src/ui/combatHud.ts`) —
 * never full page / Canvas / `.legacy-result-surface` / `.command-rail`
 * (AC5, AC7). Markup is never hand-authored here; it comes entirely from
 * `createHudController()` / `COMBAT_HUD_MARKUP` so this test can never
 * silently drift into re-implementing the production renderer.
 *
 * Issue #2014 AC2 (`data-critical-subregion`) note: `src/ui/combatHud.ts`
 * is NOT in this Issue's Allowed Paths, so a new `data-critical-subregion`
 * attribute cannot be added to production markup. `COMBAT_HUD_MARKUP`
 * already exposes `data-hud-zone="status|elapsed|edge-control|pause"` on
 * exactly the four subregion roots this Issue's `data-critical-subregion`
 * concept names — so critical-subregion selection below is done via the
 * existing `data-hud-zone` attribute (identifier values match exactly;
 * see `assertCriticalSubregionVisible()`).
 *
 * Issue #2014 AC1 calibrated absolute pixel cap (48), co-declared inline
 * at each `toMatchScreenshot()` call site below alongside
 * `allowedMismatchedPixelRatio: 0.02` (Vitest applies the stricter of the
 * two — they are not mutually exclusive). NOT hoisted into a shared
 * constant: `scripts/check-visual-artifact-pipeline.py`'s static validator
 * only accepts a plain numeric literal directly inside
 * `comparatorOptions: { ... }` and fails closed on any variable
 * reference/expression, so `48` is repeated as a literal at every call
 * site intentionally.
 *
 * Derivation (fix_delta iteration 1, PR #2024 OWNER REQUEST_CHANGES
 * comment
 * https://github.com/squne121/loop-protocol/pull/2024#issuecomment-5225055386):
 *
 * - Measured real fresh-run noise: 3 independent fresh containers of the
 *   CI-pinned canonical environment (`mcr.microsoft.com/playwright@sha256:
 *   9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948`),
 *   each running `pnpm exec vitest run --config vitest.visual.config.ts
 *   tests/component/combat-hud-running.vrt.test.ts` with a temporary
 *   `allowedMismatchedPixels: 0` (zero tolerance) against the
 *   already-committed baseline, all PASSED with 0 reported mismatched
 *   pixels — i.e. the measured fresh-run-vs-baseline noise floor in this
 *   canonical environment is 0px. `.github/workflows/ci.yml`'s
 *   `zero-tolerance-probe` step reproduces this same measurement bound to
 *   the current head on every CI run (Issue #2014 AC8), via a scoped,
 *   temporary `sed` override of this file's `48` literal to `0` for that
 *   one probe invocation only (never committed) — this keeps exactly one
 *   real, literal `toMatchScreenshot('combat-hud-running.png', ...)` call
 *   site in the committed source, which
 *   `scripts/check-visual-artifact-pipeline.py`'s static validator
 *   requires (it fails closed on duplicate or non-literal
 *   `comparatorOptions` call sites for the same screenshot name).
 * - `48` adds a safety margin above that measured 0px floor while staying
 *   far below the smallest critical subregion fragment measured via
 *   `getBoundingClientRect()` in the same canonical environment: `elapsed`
 *   ~1,240px² / `pause` ~2,712px² / `hull` field ~1,275px² / `status`
 *   cluster ~19,063px² (OWNER review comment's own rough estimates: HULL
 *   ~4,128px, Pause ~2,808px, elapsed ~1,344px — same order of magnitude).
 *   The chosen cap is ~26x smaller than the smallest of these (elapsed),
 *   so a full disappearance of any critical subregion can never be
 *   absorbed within tolerance — the env-gated mutation negative-control
 *   lane below proves this directly (measured mismatches: hull_hidden
 *   178px, pause_hidden 371px, elapsed_hidden 95px, all >> 48) rather than
 *   relying on this arithmetic alone.
 * - This is intentionally far stricter than the pre-existing
 *   `allowedMismatchedPixelRatio: 0.02` alone (~5,408px effective budget
 *   on the 1280x211 capture root), which is the defect this Issue exists
 *   to close.
 */

const NOOP_ACTIONS: HudActions = {
  onClaimReward: () => {},
  onNextSortie: () => {},
  onTogglePause: () => {},
}

let container: HTMLDivElement | null = null

beforeEach(() => {
  // GIVEN: a fresh production DOM mount per test. Vitest Browser Mode's
  // Playwright provider shares a single page across scenarios within this
  // test file, so stale DOM/focus from a previous test must never leak
  // into the next one.
  container = document.createElement('div')
  container.setAttribute('data-battle-ui-root', '')
  document.body.appendChild(container)
})

afterEach(() => {
  container?.remove()
  container = null
})

/** Shared running-phase GameState builder (hp=80/100, non-critical). */
function mountRunningScenario(root: HTMLDivElement): HTMLElement {
  const state = createInitialGameState()
  state.loopPhase = 'running'
  state.player.hp = 80
  state.player.maxHp = 100
  state.sortie = {
    status: 'running',
    elapsedTicks: 900,
    targetTicks: 1800,
    result: null,
  }

  const hud = createHudController(root, NOOP_ACTIONS)
  // activeFixedDeltaMs: 16 — fixed timestep supplied by the caller, mirrors
  // src/ui/HudController.ts's own worked example (900 ticks * 16ms = 14.4 s).
  hud.render(state, false, 16)

  const combatHudElement = root.querySelector('[data-combat-hud]')
  if (!(combatHudElement instanceof HTMLElement)) {
    throw new Error('[data-combat-hud] was not rendered by createHudController()')
  }
  return combatHudElement
}

/** Issue #2014 AC4 critical fixture (hull ratio 25/100 = 0.25 <= COMBAT_HUD_CRITICAL_HULL_RATIO). */
function mountCriticalScenario(root: HTMLDivElement): HTMLElement {
  const state = createInitialGameState()
  state.loopPhase = 'running'
  state.player.hp = 25
  state.player.maxHp = 100
  state.sortie = {
    status: 'running',
    elapsedTicks: 900,
    targetTicks: 1800,
    result: null,
  }

  const hud = createHudController(root, NOOP_ACTIONS)
  hud.render(state, false, 16)

  const combatHudElement = root.querySelector('[data-combat-hud]')
  if (!(combatHudElement instanceof HTMLElement)) {
    throw new Error('[data-combat-hud] was not rendered by createHudController()')
  }
  return combatHudElement
}

test('GIVEN the combat-hud-running scenario WHEN [data-combat-hud] is captured THEN it matches the committed provisional baseline (AC5, AC6, AC7)', async () => {
  if (!container) {
    throw new Error('container was not mounted by beforeEach')
  }

  const combatHudElement = mountRunningScenario(container)

  // Deterministic font metrics before capture (Outcome).
  await document.fonts.ready

  // Issue #2014 AC7/AC8: `pnpm test:vrt:evidence:triple-run` runs this spec
  // 3x in fresh Node/browser processes with VITE_VRT_EVIDENCE_CAPTURE=true
  // and a distinct VITE_VRT_EVIDENCE_RUN_ID each time. Each fresh process
  // saves its own actual-rendered PNG (independent of match/mismatch
  // outcome) under `tests/component/.vrt-evidence/run-<id>/`, a
  // dot-prefixed, never-committed directory (mirrors the existing
  // `.vitest-attachments/` convention). The triple-run script then hashes
  // and compares all 3 to prove real render determinism across fresh
  // processes bound to the current head — not merely that the committed
  // baseline FILE is unchanged by a non-`--update` run.
  if (import.meta.env.VITE_VRT_EVIDENCE_CAPTURE === 'true') {
    const runId = import.meta.env.VITE_VRT_EVIDENCE_RUN_ID ?? 'unlabeled'
    await page.screenshot({
      element: combatHudElement,
      path: `.vrt-evidence/run-${runId}/combat-hud-running-actual.png`,
      save: true,
    })
  }

  // THEN: matches the committed provisional baseline. AC7: capture root is
  // [data-combat-hud] only — never full page / Canvas / legacy result
  // surface / command rail. AC1 (Issue #2014): calibrated absolute pixel
  // cap co-declared with the pre-existing ratio (see the derivation
  // comment above).
  await expect(page.elementLocator(combatHudElement)).toMatchScreenshot('combat-hud-running.png', {
    comparatorOptions: {
      allowedMismatchedPixelRatio: 0.02,
      allowedMismatchedPixels: 48,
    },
  })
})

test('GIVEN the combat-hud-critical scenario (hp=25/maxHp=100) WHEN [data-combat-hud] is captured THEN it matches the committed critical-fixture baseline (AC4, AC5)', async () => {
  if (!container) {
    throw new Error('container was not mounted by beforeEach')
  }

  const combatHudElement = mountCriticalScenario(container)
  await document.fonts.ready

  // Sanity precondition for this fixture's own intent (AC4): the critical
  // warning fragment must actually be rendered and visible before its
  // baseline is captured, otherwise this fixture silently fails to be a
  // critical-warning fixture at all.
  const criticalEl = container.querySelector('[data-field="combat-hud-critical"]')
  if (!(criticalEl instanceof HTMLElement)) {
    throw new Error('[data-field="combat-hud-critical"] was not rendered for the hp=25/maxHp=100 fixture')
  }
  expect(criticalEl.hidden).toBe(false)

  await expect(page.elementLocator(combatHudElement)).toMatchScreenshot('combat-hud-critical.png', {
    comparatorOptions: {
      allowedMismatchedPixelRatio: 0.02,
      allowedMismatchedPixels: 48,
    },
  })
})

/**
 * Issue #2014 AC2: individual small visual assertion per critical
 * subregion, selected via `data-hud-zone` (documented equivalence to
 * `data-critical-subregion` above). Deliberately NOT `toBeVisible()`
 * (Vitest's own visibility definition treats `opacity: 0` as visible —
 * Issue #2014 body) — uses computed-style + rendered bounding-box
 * assertions instead, and deliberately does NOT introduce new committed
 * screenshot baselines per subregion (AC5 only requires regenerating the
 * running + critical-fixture baselines).
 */
test('GIVEN the combat-hud-running scenario WHEN each critical subregion is inspected THEN status/elapsed/pause/edge-control are individually rendered with non-zero visible area (AC2)', async () => {
  if (!container) {
    throw new Error('container was not mounted by beforeEach')
  }

  mountRunningScenario(container)
  await document.fonts.ready

  const criticalSubregions = ['status', 'elapsed', 'pause', 'edge-control'] as const
  for (const subregion of criticalSubregions) {
    const el = container.querySelector(`[data-hud-zone="${subregion}"]`)
    if (!(el instanceof HTMLElement)) {
      throw new Error(`critical subregion [data-hud-zone="${subregion}"] was not rendered`)
    }
    const style = getComputedStyle(el)
    const rect = el.getBoundingClientRect()
    expect(style.display, `${subregion}: display`).not.toBe('none')
    expect(style.visibility, `${subregion}: visibility`).not.toBe('hidden')
    expect(Number(style.opacity), `${subregion}: opacity`).toBeGreaterThan(0)
    expect(rect.width, `${subregion}: width`).toBeGreaterThan(0)
    expect(rect.height, `${subregion}: height`).toBeGreaterThan(0)
  }
})

// ---------------------------------------------------------------------------
// Issue #2014 AC3: env-gated mutation negative-control lane. Proves the
// comparator (AC1's calibrated cap) actually has detection power by
// deliberately hiding HULL / Pause / elapsed ONE AT A TIME and asserting
// the SAME running-scenario `toMatchScreenshot()` call against the SAME
// committed baseline REJECTS (fails) with a mismatch. Gated behind
// dedicated env vars (mirrors
// `tests/component/__negative_control__/hidden-attachment-negative-control.vrt.test.ts`'s
// `VITE_VRT_NEGATIVE_CONTROL` pattern) so these never run — and never
// false-pass/false-fail — during a normal `pnpm test:vrt:component`
// invocation. Each control is independently gated so CI can report a
// per-target `expected_failure` result (Issue #2014 AC8 evidence
// manifest's `mutation_controls: { hull_hidden, pause_hidden,
// elapsed_hidden }`).
//
// Deliberately written as `expect(<promise>).rejects.toThrow()` (not a
// bare `await expect(locator).toMatchScreenshot(...)` + try/catch): this
// is the same guard idiom already used by
// `tests/e2e/visual-scenario-fixture.spec.ts`'s own negative-control
// assertions, and `scripts/check-visual-artifact-pipeline.py`'s static
// validator explicitly recognises `.rejects` as a guard-only call site
// (never counted as a second real production capture of
// 'combat-hud-running.png' — the static validator hard-fails on duplicate
// capture ids for the same screenshot name otherwise).
// ---------------------------------------------------------------------------

const RUN_MUTATION_CONTROL_HULL = import.meta.env.VITE_VRT_MUTATION_CONTROL_HULL === 'true'
const RUN_MUTATION_CONTROL_PAUSE = import.meta.env.VITE_VRT_MUTATION_CONTROL_PAUSE === 'true'
const RUN_MUTATION_CONTROL_ELAPSED = import.meta.env.VITE_VRT_MUTATION_CONTROL_ELAPSED === 'true'

test.skipIf(!RUN_MUTATION_CONTROL_HULL)(
  'GIVEN HULL is deliberately hidden (visibility:hidden) WHEN captured against the committed baseline THEN toMatchScreenshot() rejects with a non-zero mismatch (AC3 mutation negative control — hull_hidden)',
  async () => {
    if (!container) {
      throw new Error('container was not mounted by beforeEach')
    }
    const combatHudElement = mountRunningScenario(container)
    const hullEl = container.querySelector('[data-field="combat-hud-hull"]')
    if (!(hullEl instanceof HTMLElement)) {
      throw new Error('[data-field="combat-hud-hull"] was not rendered')
    }
    hullEl.style.visibility = 'hidden'
    await document.fonts.ready

    await expect(
      expect(page.elementLocator(combatHudElement)).toMatchScreenshot('combat-hud-running.png', {
        comparatorOptions: {
          allowedMismatchedPixelRatio: 0.02,
          allowedMismatchedPixels: 48,
        },
      }),
    ).rejects.toThrow()
  },
)

test.skipIf(!RUN_MUTATION_CONTROL_PAUSE)(
  'GIVEN Pause is deliberately hidden (visibility:hidden) WHEN captured against the committed baseline THEN toMatchScreenshot() rejects with a non-zero mismatch (AC3 mutation negative control — pause_hidden)',
  async () => {
    if (!container) {
      throw new Error('container was not mounted by beforeEach')
    }
    const combatHudElement = mountRunningScenario(container)
    const pauseEl = container.querySelector('[data-hud-zone="pause"]')
    if (!(pauseEl instanceof HTMLElement)) {
      throw new Error('[data-hud-zone="pause"] was not rendered')
    }
    pauseEl.style.visibility = 'hidden'
    await document.fonts.ready

    await expect(
      expect(page.elementLocator(combatHudElement)).toMatchScreenshot('combat-hud-running.png', {
        comparatorOptions: {
          allowedMismatchedPixelRatio: 0.02,
          allowedMismatchedPixels: 48,
        },
      }),
    ).rejects.toThrow()
  },
)

test.skipIf(!RUN_MUTATION_CONTROL_ELAPSED)(
  'GIVEN elapsed is deliberately hidden (visibility:hidden) WHEN captured against the committed baseline THEN toMatchScreenshot() rejects with a non-zero mismatch (AC3 mutation negative control — elapsed_hidden)',
  async () => {
    if (!container) {
      throw new Error('container was not mounted by beforeEach')
    }
    const combatHudElement = mountRunningScenario(container)
    const elapsedEl = container.querySelector('[data-hud-zone="elapsed"]')
    if (!(elapsedEl instanceof HTMLElement)) {
      throw new Error('[data-hud-zone="elapsed"] was not rendered')
    }
    elapsedEl.style.visibility = 'hidden'
    await document.fonts.ready

    await expect(
      expect(page.elementLocator(combatHudElement)).toMatchScreenshot('combat-hud-running.png', {
        comparatorOptions: {
          allowedMismatchedPixelRatio: 0.02,
          allowedMismatchedPixels: 48,
        },
      }),
    ).rejects.toThrow()
  },
)
