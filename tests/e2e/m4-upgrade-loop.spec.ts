/**
 * E2E: M4 upgrade loop (Issue #1283)
 *
 * Reproduces the regular player-facing navigation for the full M4 upgrade
 * loop close evidence: Load Game (seeded snapshot) -> Upgrade weapon
 * purchase -> reload -> Load Game (restore) -> Launch sortie -> fire, and
 * asserts the storage-namespace / observability-hook boundaries the loop
 * depends on.
 *
 * Covers:
 * AC1: E2E-only key seeded with a valid snapshot
 *      ({ resources: 100, weaponPower: 1, playerMaxHp: 8 }).
 * AC2: title_menu -> Load Game (regular navigation) shows the seeded
 *      snapshot in the HUD.
 * AC3: Upgrade weapon purchase: resources 100 -> 0, weaponPower 1 -> 2 in
 *      both HUD and the saved snapshot.
 * AC4: reload preserves the E2E key snapshot (post-purchase values).
 * AC5: Load Game after reload restores weaponPower 2 to both runtime and
 *      HUD.
 * AC6: Launch sortie + canvas pointer input fires a new projectile whose
 *      damage is 2 (weaponPower snapshot at fire time).
 * AC7: the production sentinel key is byte-for-byte unchanged throughout.
 * AC8: only the E2E-scoped key is written during the scenario.
 * AC10: `__LOOP_E2E__` is read-only — it exposes `getState()` only, and the
 *       returned snapshot never carries mutation methods.
 *
 * Bootstrap fixture (Issue #1283 Design Constraints):
 * - `window.__LOOP_E2E_BOOTSTRAP__ = { autoStart: false }` disables the
 *   legacy VITE_E2E_MODE auto-start (title_menu -> preparation -> running)
 *   so this scenario can drive the normal player-facing navigation
 *   (Load Game / Upgrade weapon / Launch sortie) instead.
 * - The seed key is written only when absent, so a reload does not
 *   overwrite the post-purchase snapshot with the original seed (per the
 *   Design Constraints "storage seed only writes when the key is absent").
 */

import { test, expect, type Page } from '@playwright/test'

// ---------------------------------------------------------------------------
// Helper: read-only __LOOP_E2E__ observability hook
// ---------------------------------------------------------------------------

interface LoopE2ESnapshot {
  tick: number
  elapsedMs: number
  loopPhase:
    | 'title_menu'
    | 'load_menu'
    | 'preparation'
    | 'running'
    | 'result'
    | 'debrief_pending_reward'
    | 'debrief_reward_claimed'
  progress: {
    resources: number
    weaponPower: number
  }
  projectiles: Array<{
    id: number
    x: number
    y: number
    ageMs: number
    damage: number
  }>
}

async function getGameState(page: Page): Promise<LoopE2ESnapshot> {
  return page.evaluate(() => {
    const hook = (
      window as Window & {
        __LOOP_E2E__?: { getState: () => LoopE2ESnapshot }
      }
    ).__LOOP_E2E__
    if (!hook) {
      throw new Error(
        '__LOOP_E2E__ hook not found. Was the app built with VITE_E2E_MODE=true?',
      )
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return hook.getState() as any
  })
}

function buildE2EStorageKey(testInfo: ReturnType<typeof test.info>): string {
  const workerScope = `worker-${testInfo.workerIndex}.retry-${testInfo.retry}.test-${testInfo.testId.replace(/[^a-zA-Z0-9._-]/g, '-')}`
  return `loop-protocol.e2e.${workerScope}.mvp.save`
}

const PRODUCTION_KEY = 'loop-protocol.mvp.save'
const PRODUCTION_SENTINEL = JSON.stringify({
  schemaVersion: 1,
  resources: 777,
  weaponPower: 3,
  playerMaxHp: 11,
})

type StoredSnapshot = {
  schemaVersion: number
  resources: number
  weaponPower: number
  playerMaxHp: number
}

async function readStorageKey(page: Page, key: string): Promise<StoredSnapshot | null> {
  const raw = await page.evaluate((k: string) => window.localStorage.getItem(k), key)
  if (raw === null) return null
  return JSON.parse(raw) as StoredSnapshot
}

/**
 * Installs the bootstrap fixture (production sentinel + E2E key override +
 * autoStart:false) before navigation. The E2E key is only seeded when it is
 * not already present, so a subsequent reload preserves the previously
 * saved (post-purchase) snapshot instead of clobbering it (AC4).
 */
async function installBootstrapFixture(
  page: Page,
  payload: { productionKey: string; productionSentinel: string; e2eKey: string },
): Promise<void> {
  await page.addInitScript(
    (init: { productionKey: string; productionSentinel: string; e2eKey: string }) => {
      // Production sentinel: constant reference fixture, safe to re-apply on every load.
      window.localStorage.setItem(init.productionKey, init.productionSentinel)

      // AC1 / reload-safe seed: only write the E2E key if it does not already
      // exist, so a reload after a purchase does not overwrite the saved state.
      if (window.localStorage.getItem(init.e2eKey) === null) {
        window.localStorage.setItem(
          init.e2eKey,
          JSON.stringify({
            schemaVersion: 1,
            resources: 100,
            weaponPower: 1,
            playerMaxHp: 8,
          }),
        )
      }

      // Runtime storage-key override (E2E lane — AC8).
      ;(window as Window & { __LOOP_STORAGE_KEY__?: string }).__LOOP_STORAGE_KEY__ = init.e2eKey

      // Pre-bootstrap fixture (Issue #1283 Design Constraints): navigation-time
      // config, read once at module init — not a live mutation API.
      ;(
        window as Window & { __LOOP_E2E_BOOTSTRAP__?: { autoStart?: boolean } }
      ).__LOOP_E2E_BOOTSTRAP__ = { autoStart: false }
    },
    payload,
  )
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

test(
  'M4 upgrade loop: GIVEN seeded snapshot WHEN Load Game -> Upgrade weapon -> reload -> Load Game -> Launch sortie -> fire THEN persistence and projectile damage reflect the purchased weaponPower',
  async ({ page }) => {
    test.setTimeout(60_000)

    const testInfo = test.info()
    const e2eKey = buildE2EStorageKey(testInfo)

    await installBootstrapFixture(page, {
      productionKey: PRODUCTION_KEY,
      productionSentinel: PRODUCTION_SENTINEL,
      e2eKey,
    })
    await page.goto('/')

    // AC10: __LOOP_E2E__ is read-only — getState() only, no mutation methods.
    const hookShape = await page.evaluate(() => {
      const hook = (window as Window & { __LOOP_E2E__?: Record<string, unknown> }).__LOOP_E2E__
      if (!hook) return null
      return Object.keys(hook)
    })
    expect(hookShape, '__LOOP_E2E__ must exist (AC10)').not.toBeNull()
    expect(hookShape, '__LOOP_E2E__ must expose only getState() (AC10)').toEqual(['getState'])

    // AC1 / bootstrap: autoStart disabled — app stays at title_menu, not auto-advanced.
    const initialState = await getGameState(page)
    expect(initialState.loopPhase, 'app must stay at title_menu when autoStart is disabled').toBe(
      'title_menu',
    )

    // AC2: title menu -> Load Game (two-step navigation: open load menu, then apply).
    const loadGameButton = page.locator('[data-action="load-game"]')
    await expect(loadGameButton).toBeEnabled({ timeout: 5_000 })
    await loadGameButton.click()
    await expect(page.locator('[data-field="status"]')).toHaveText('Load Menu.', {
      timeout: 5_000,
    })
    await loadGameButton.click()
    await expect(page.locator('[data-field="status"]')).toHaveText('Load Game complete.', {
      timeout: 5_000,
    })

    // AC2: HUD shows the seeded snapshot.
    await expect(page.locator('[data-field="resources"]')).toHaveText('100', { timeout: 5_000 })
    await expect(page.locator('[data-field="weapon-power"]')).toHaveText('1', { timeout: 5_000 })

    const stateAfterLoad = await getGameState(page)
    expect(stateAfterLoad.loopPhase, 'phase must be preparation after Load Game (AC2)').toBe(
      'preparation',
    )
    expect(stateAfterLoad.progress.resources, 'runtime resources must reflect seed (AC2)').toBe(
      100,
    )
    expect(
      stateAfterLoad.progress.weaponPower,
      'runtime weaponPower must reflect seed (AC2)',
    ).toBe(1)

    // AC3: Upgrade weapon purchase.
    const upgradeButton = page.locator('[data-action="upgrade-weapon"]')
    await expect(upgradeButton).toBeEnabled({ timeout: 5_000 })
    await upgradeButton.click()

    await expect(page.locator('[data-field="resources"]')).toHaveText('0', { timeout: 5_000 })
    await expect(page.locator('[data-field="weapon-power"]')).toHaveText('2', { timeout: 5_000 })

    const savedAfterPurchase = await readStorageKey(page, e2eKey)
    expect(savedAfterPurchase, 'E2E key must hold a snapshot after purchase (AC3)').not.toBeNull()
    expect(savedAfterPurchase!.resources, 'saved resources must be 0 after purchase (AC3)').toBe(0)
    expect(
      savedAfterPurchase!.weaponPower,
      'saved weaponPower must be 2 after purchase (AC3)',
    ).toBe(2)

    // AC7: production sentinel must be unchanged so far.
    const productionAfterPurchase = await page.evaluate(
      (key: string) => window.localStorage.getItem(key),
      PRODUCTION_KEY,
    )
    expect(
      productionAfterPurchase,
      'production key must remain unchanged after purchase (AC7)',
    ).toBe(PRODUCTION_SENTINEL)

    // AC4: reload — the bootstrap fixture re-runs but must NOT overwrite the
    // already-saved E2E key (seed only writes when absent).
    await page.reload()

    const stateAfterReload = await getGameState(page)
    expect(
      stateAfterReload.loopPhase,
      'app must stay at title_menu after reload (autoStart disabled)',
    ).toBe('title_menu')

    const savedAfterReload = await readStorageKey(page, e2eKey)
    expect(savedAfterReload, 'E2E key must persist across reload (AC4)').not.toBeNull()
    expect(savedAfterReload!.resources, 'saved resources must persist across reload (AC4)').toBe(
      0,
    )
    expect(
      savedAfterReload!.weaponPower,
      'saved weaponPower must persist across reload (AC4)',
    ).toBe(2)

    // AC5: Load Game after reload restores weaponPower 2 to runtime and HUD.
    const loadGameButtonAfterReload = page.locator('[data-action="load-game"]')
    await expect(loadGameButtonAfterReload).toBeEnabled({ timeout: 5_000 })
    await loadGameButtonAfterReload.click()
    await expect(page.locator('[data-field="status"]')).toHaveText('Load Menu.', {
      timeout: 5_000,
    })
    await loadGameButtonAfterReload.click()
    await expect(page.locator('[data-field="status"]')).toHaveText('Load Game complete.', {
      timeout: 5_000,
    })

    await expect(page.locator('[data-field="weapon-power"]')).toHaveText('2', { timeout: 5_000 })

    const stateAfterRestore = await getGameState(page)
    expect(
      stateAfterRestore.progress.weaponPower,
      'runtime weaponPower must be restored to 2 (AC5)',
    ).toBe(2)
    expect(
      stateAfterRestore.loopPhase,
      'phase must be preparation after restore Load Game (AC5)',
    ).toBe('preparation')

    // AC6: Launch sortie + canvas pointer input fires a projectile with damage 2.
    const startSortieButton = page.locator('[data-action="start-sortie"]')
    await expect(startSortieButton).toBeEnabled({ timeout: 5_000 })
    await startSortieButton.click()

    await expect
      .poll(
        async () => {
          const s = await getGameState(page)
          return s.loopPhase
        },
        { timeout: 5_000, intervals: [50] },
      )
      .toBe('running')

    const canvas = page.locator('canvas.battle-stage__canvas')
    const box = await canvas.boundingBox()
    expect(box, 'canvas must be visible before firing (AC6)').not.toBeNull()
    const centerX = box!.x + box!.width / 2
    const centerY = box!.y + box!.height / 2

    await page.mouse.move(centerX, centerY)
    await page.mouse.down({ button: 'left' })

    await expect
      .poll(
        async () => {
          const s = await getGameState(page)
          return s.projectiles.length
        },
        { timeout: 3_000, intervals: [50] },
      )
      .toBeGreaterThan(0)

    const stateWithProjectile = await getGameState(page)
    await page.mouse.up()

    expect(
      stateWithProjectile.projectiles[0]!.damage,
      'newly fired projectile damage must equal the restored weaponPower (AC6)',
    ).toBe(2)

    // AC7: production sentinel must still be unchanged at the end of the scenario.
    const productionFinal = await page.evaluate(
      (key: string) => window.localStorage.getItem(key),
      PRODUCTION_KEY,
    )
    expect(
      productionFinal,
      'production key must remain byte-for-byte unchanged throughout the scenario (AC7)',
    ).toBe(PRODUCTION_SENTINEL)

    // AC8: only the E2E-scoped key (and the production sentinel, which was
    // pre-seeded and never mutated by the app) exist among the two keys this
    // scenario touches; no third key was created by the app.
    const allKeys = await page.evaluate(() => {
      const keys: string[] = []
      for (let i = 0; i < window.localStorage.length; i += 1) {
        const k = window.localStorage.key(i)
        if (k !== null) keys.push(k)
      }
      return keys.sort()
    })
    expect(
      allKeys,
      'only the production sentinel and the E2E-scoped key must exist (AC8)',
    ).toEqual([PRODUCTION_KEY, e2eKey].sort())
  },
)
