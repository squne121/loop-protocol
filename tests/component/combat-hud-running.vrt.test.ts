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
 * component VRT scaffold (Issue #1389, `#1380` VRT rollout tracker).
 *
 * Report-only lane: backs the non-required `component-vrt-report` CI job
 * only (In Scope / Out of Scope). `combat-hud-running` is the ONLY
 * scenario in this Issue's Current Validated Scope — result / pause modal
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

test('GIVEN the combat-hud-running scenario WHEN [data-combat-hud] is captured THEN it matches the committed provisional baseline (AC5, AC6, AC7)', async () => {
  if (!container) {
    throw new Error('container was not mounted by beforeEach')
  }

  // WHEN: deterministic running-phase GameState (Outcome: deterministic
  // GameState, fixed timestep — never wall-clock/rAF-derived values).
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

  const hud = createHudController(container, NOOP_ACTIONS)
  // activeFixedDeltaMs: 16 — fixed timestep supplied by the caller, mirrors
  // src/ui/HudController.ts's own worked example (900 ticks * 16ms = 14.4 s).
  hud.render(state, false, 16)

  const combatHudElement = container.querySelector('[data-combat-hud]')
  if (!(combatHudElement instanceof HTMLElement)) {
    throw new Error('[data-combat-hud] was not rendered by createHudController()')
  }

  // Deterministic font metrics before capture (Outcome).
  await document.fonts.ready

  // THEN: matches the committed provisional baseline. AC7: capture root is
  // [data-combat-hud] only — never full page / Canvas / legacy result
  // surface / command rail.
  await expect(page.elementLocator(combatHudElement)).toMatchScreenshot('combat-hud-running.png', {
    comparatorOptions: {
      allowedMismatchedPixelRatio: 0.02,
    },
  })
})
