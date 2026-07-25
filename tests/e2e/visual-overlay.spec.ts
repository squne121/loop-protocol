/**
 * tests/e2e/visual-overlay.spec.ts — Playwright primary VRT suite (Issue
 * #1386, #1380 rollout tracker).
 *
 * This is the primary browser/runtime VRT gate (`pnpm test:vrt:e2e` /
 * `pnpm test:vrt`), scoped to the DOM overlay root (`[data-battle-ui-root]`,
 * `.battle-ui-layer[data-battle-ui-root]` in `src/main.ts`) — never a
 * full-canvas / full-body / full-shell target, and never `.command-rail`
 * (AC3, AC5). It reuses the Scenario Support Matrix, Screenshot Target
 * Guard and freeze CSS wiring introduced in Issue #1385
 * (`tests/e2e/visual-utils.ts`) and the maturity classification in
 * `docs/dev/visual-baseline-registry.md` (Issue #1384).
 *
 * Out of scope for this Issue (see `docs/dev/visual-baseline-registry.md`
 * and the Issue #1386 contract): adding a `frozen` baseline for any final
 * overlay surface, and implementing the `running-hud-paused` /
 * `result-timeout` / `final-no-command-rail` overlay UIs themselves
 * (#1375 / #1376 / #1377). Those scenarios remain `pending-fixture`
 * (`VISUAL_SCENARIO_STATUS`) / `pending-baseline` (registry `maturity`)
 * here and are only exercised as explicit-pending / fail-closed proofs
 * (AC4) — never captured.
 */

import { test, expect } from '@playwright/test'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  installVisualScenario,
  expectDomOverlayScreenshot,
  isPendingFixtureScenario,
  VISUAL_BASELINE_REGISTRY_MATURITY,
  type VisualScenarioFixture,
} from './visual-utils'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** `active-fixture-only` (Scenario Support Matrix) — safe to install and capture. */
const RUNNING_HUD_FIXTURE: VisualScenarioFixture = {
  name: 'running-hud',
  loopPhase: 'running',
  paused: false,
  sortie: { status: 'running', elapsedTicks: 900, fixedDeltaMs: 16 },
  player: { hp: 80, maxHp: 100 },
  progress: { resources: 12, weaponPower: 1 },
  telemetry: { status: '', summary: '' },
  viewportLabel: 'desktop-1280x720',
}

/**
 * `pending-fixture` fixture payload, used only to prove the fail-closed
 * rejection below (AC4) — never installed successfully and never used to
 * capture a screenshot.
 */
const RUNNING_HUD_PAUSED_FIXTURE: VisualScenarioFixture = {
  name: 'running-hud-paused',
  loopPhase: 'running',
  paused: true,
  sortie: { status: 'running', elapsedTicks: 900, fixedDeltaMs: 16 },
  player: { hp: 80, maxHp: 100 },
  progress: { resources: 12, weaponPower: 1 },
  telemetry: { status: '', summary: '' },
  viewportLabel: 'desktop-1280x720',
}

// ---------------------------------------------------------------------------
// Primary VRT gate: DOM overlay root, provisional/legacy-current baseline only
// ---------------------------------------------------------------------------
//
// This suite is the "Playwright primary VRT suite" (Issue #1386 Outcome):
// it runs with pending/provisional baselines only. The one active capture
// below targets the `[data-battle-ui-root]` DOM overlay layer for the
// `running-hud` scenario (`docs/dev/visual-baseline-registry.md`'s
// `running-hud` row is `legacy-current`, not `frozen`) — it is not a
// `.command-rail` capture (AC3: `.command-rail` is a separate,
// `aside.command-rail` legacy element outside the DOM overlay root and is
// never the target of this suite) and is not a `frozen` final overlay
// baseline (Out of Scope).
//
// `expectDomOverlayScreenshot()` (tests/e2e/visual-utils.ts) applies the
// shared freeze CSS via Playwright's `stylePath` option (AC6) and masks
// every `canvas` element so the DOM overlay capture never bleeds in Canvas
// battle-stage pixels (AC5's "Canvas is only included via mask or an
// explicit registered canvas visual cue baseline" — this capture uses the
// `mask` path, not a Canvas visual cue exception).

test('GIVEN the running-hud active-fixture-only scenario WHEN the DOM overlay root is captured THEN it matches the provisional/legacy-current baseline (AC5, AC6)', async ({
  page,
}) => {
  await installVisualScenario(page, RUNNING_HUD_FIXTURE)
  await page.goto('/')

  const overlayRoot = page.locator('[data-battle-ui-root]')
  // registryId + explicit maxDiffPixels (Issue #1386 PR #1721 review fix, P1
  // Blocker 1 / Blocker 2): `running-hud-overlay-legacy-current` is a
  // distinct registry row from `running-hud` (docs/dev/visual-baseline-registry.md),
  // and the tolerance is an absolute pixel budget (not a ratio of the full
  // masked capture) measured empirically against this capture root — see
  // the registry row's `tolerance` column for the measurement method.
  await expectDomOverlayScreenshot(overlayRoot, 'vrt-running-hud-overlay.png', 'running-hud-overlay-legacy-current', {
    maxDiffPixels: 100,
  })
})

// ---------------------------------------------------------------------------
// Pending-baseline scenarios (AC4)
// ---------------------------------------------------------------------------
//
// `running-hud-paused` / `result-overlay-timeout` / `final-no-command-rail`
// (docs/dev/visual-baseline-registry.md registry ids; `result-timeout` is
// the corresponding `VisualScenarioName`) are `pending-baseline` in the
// registry and `pending-fixture` in the Scenario Support Matrix — their
// overlay surface implementation child issues (#1375 / #1376 / #1377) have
// not merged. This primary VRT suite marks them `pending-baseline`
// explicitly via `test.skip()` rather than silently omitting them or
// capturing a premature baseline of the current pre-overlay UI.

const PENDING_SCENARIO_REGISTRY_IDS = {
  'running-hud-paused': 'running-hud-paused',
  'result-timeout': 'result-overlay-timeout',
  'final-no-command-rail': 'final-no-command-rail',
} as const

/**
 * Committed screenshot baselines directory for this spec file
 * (`playwright.config.ts`'s `snapshotPathTemplate`). Used only for the
 * registry-drift guard below (Issue #1386 PR #1721 review fix, P2 Blocker
 * #6) — never to decide what to capture.
 */
const SCREENSHOTS_DIR = fileURLToPath(new URL('./__screenshots__/visual-overlay.spec.ts/', import.meta.url))

/**
 * Registry-drift guard (Issue #1386 PR #1721 review fix, P2 Blocker #6).
 * The `test.skip()` loop below is a hand-authored constant describing
 * `docs/dev/visual-baseline-registry.md`'s current pending-baseline rows —
 * it does not automatically re-derive from the registry doc or from
 * `VISUAL_SCENARIO_STATUS`. Without a check, this constant could silently
 * drift from the registry (e.g. a future PR promotes a registry row to
 * `legacy-current` / `frozen` and adds a committed PNG/active test, but
 * forgets to remove the entry here) and this suite would keep skipping a
 * scenario that should now be actively captured, instead of failing loudly.
 * This asserts, for every entry above:
 *   1. `VISUAL_BASELINE_REGISTRY_MATURITY[registryId]` is still
 *      `pending-baseline` (registry says pending).
 *   2. no baseline PNG for that registry id is committed under
 *      `tests/e2e/__screenshots__/visual-overlay.spec.ts/` (no premature
 *      capture exists that the registry doesn't know about).
 * Both directions fail closed: if either check fails, this suite fails
 * loudly instead of silently continuing to skip a scenario that is (or
 * should be) active.
 */
for (const [scenarioName, registryId] of Object.entries(PENDING_SCENARIO_REGISTRY_IDS)) {
  test(`GIVEN the ${scenarioName} scenario is pending-baseline (registry id: ${registryId}) WHEN this primary VRT suite runs THEN it is skipped explicitly (AC4)`, () => {
    const maturity = VISUAL_BASELINE_REGISTRY_MATURITY[registryId]
    expect(
      maturity,
      `registry/test drift: "${registryId}" is hard-coded pending here but ` +
        `docs/dev/visual-baseline-registry.md / VISUAL_BASELINE_REGISTRY_MATURITY reports maturity ` +
        `"${maturity}" — update this suite (and stop skipping) instead of leaving it silently pending.`,
    ).toBe('pending-baseline')

    const candidatePngPaths = [
      `${SCREENSHOTS_DIR}vrt-${scenarioName}-overlay.png`,
      `${SCREENSHOTS_DIR}vrt-${registryId}.png`,
    ]
    const committedPng = candidatePngPaths.find((path) => existsSync(path))
    expect(
      committedPng,
      `registry/test drift: "${registryId}" is pending-baseline but a baseline PNG already exists ` +
        `at ${String(committedPng)} — the registry says "pending: no PNG/test" (§2/§3 of ` +
        'docs/dev/visual-baseline-registry.md); either the PNG is stale and must be removed, or the ' +
        'registry/test must be promoted together.',
    ).toBeUndefined()

    test.skip(
      true,
      `pending-baseline: "${registryId}" is pending-baseline in ` +
        'docs/dev/visual-baseline-registry.md (no committed PNG/test) — its overlay surface ' +
        'implementation child issue has not merged. See #1375/#1376/#1377.',
    )
  })
}

test('GIVEN the running-hud-paused pending-fixture scenario WHEN installVisualScenario is called THEN it fails closed instead of silently freezing the current pre-overlay UI (AC4)', async ({
  page,
}) => {
  expect(isPendingFixtureScenario(RUNNING_HUD_PAUSED_FIXTURE.name)).toBe(true)
  await expect(installVisualScenario(page, RUNNING_HUD_PAUSED_FIXTURE)).rejects.toThrow(
    /pending-fixture/,
  )
})
