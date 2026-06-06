/**
 * E2E: M2 combat MVP playtest (Issue #490)
 *
 * Covers AC4 behavioral assertions:
 * - sortie starts in 'running' status after app load
 * - enemies spawn (enemies.length > 0)
 * - enemy approaches player over time (distance decreases)
 * - pointer down → projectile generated (shotsFired increases)
 * - enemy can receive damage (hp decreases or defeatedAtTick set)
 * - player.hp can change due to enemy contact damage
 *
 * NOTE: Automatic E2E results confirm integration correctness only.
 * Human playtesting remains required to evaluate UX (see docs/playtest/m2-combat-mvp.md).
 */

import { test, expect, type Page } from '@playwright/test'

// ---------------------------------------------------------------------------
// Helper: access the read-only __LOOP_E2E__ observability hook
// ---------------------------------------------------------------------------

interface LoopE2EState {
  tick: number
  elapsedMs: number
  player: {
    x: number
    y: number
    aimX: number
    aimY: number
    hp: number
    maxHp: number
  }
  projectiles: Array<{
    id: number
    x: number
    y: number
    ageMs: number
  }>
  input: {
    primaryPressed: boolean
    activePointerId: number | null
  }
  enemies: Array<{
    id: number
    x: number
    y: number
    hp: number
    maxHp: number
    defeatedAtTick: number | null
  }>
  sortie: {
    status: 'idle' | 'running' | 'victory' | 'defeat' | 'ended'
    elapsedTicks: number
    result: 'victory' | 'defeat' | null
  }
  arena: {
    width: number
    height: number
  }
}

async function getGameState(page: Page): Promise<LoopE2EState> {
  return page.evaluate(() => {
    const hook = (
      window as Window & {
        __LOOP_E2E__?: { getState: () => LoopE2EState }
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

/** Wait for at least N simulation ticks to advance from a given tick. */
async function waitForTicks(
  page: Page,
  fromTick: number,
  count = 1,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.tick
      },
      { timeout: 5000, intervals: [50] },
    )
    .toBeGreaterThan(fromTick + count - 1)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.beforeEach(async ({ page }) => {
  await page.goto('/')
})

test('GIVEN app loaded WHEN sortie bootstrap runs THEN sortie.status is running', async ({
  page,
}) => {
  // GIVEN the app has loaded with M2 bootstrap
  // WHEN we query sortie state after a few ticks
  // THEN sortie.status should be 'running' (startSortie called at boot)
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBe('running')
})

test('GIVEN sortie running WHEN enemies spawn THEN enemies array is non-empty', async ({
  page,
}) => {
  // GIVEN the sortie is running
  // WHEN enough ticks elapse for enemy spawn
  // THEN enemies.length should be greater than 0
  const initial = await getGameState(page)

  // Ensure sortie running first
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBe('running')

  // Wait up to 10 seconds for at least one enemy to appear
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.enemies.length
      },
      { timeout: 10000, intervals: [100] },
    )
    .toBeGreaterThan(0)

  expect(initial).toBeDefined()
})

test('GIVEN enemy spawned WHEN ticks elapse THEN enemy approaches player (distance decreases)', async ({
  page,
}) => {
  // GIVEN an enemy has spawned and is moving toward the player
  // WHEN several ticks pass
  // THEN the distance between enemy and player should decrease

  // Wait for at least one alive enemy to appear
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.enemies.filter((e) => e.defeatedAtTick === null).length
      },
      { timeout: 10000, intervals: [100] },
    )
    .toBeGreaterThan(0)

  const stateWithEnemy = await getGameState(page)
  // Use the first alive enemy as the target
  const aliveEnemy = stateWithEnemy.enemies.find((e) => e.defeatedAtTick === null)!
  const player0 = stateWithEnemy.player

  const dist0 = Math.hypot(aliveEnemy.x - player0.x, aliveEnemy.y - player0.y)

  // Wait for 60 more ticks (~1 second at 60Hz)
  await waitForTicks(page, stateWithEnemy.tick, 60)

  const stateLater = await getGameState(page)
  const enemyLater = stateLater.enemies.find((e) => e.id === aliveEnemy.id)

  if (!enemyLater || enemyLater.defeatedAtTick !== null) {
    // Target enemy was defeated — find a different alive enemy and check it approached
    const anotherAlive = stateLater.enemies.find((e) => e.defeatedAtTick === null)
    if (!anotherAlive) {
      // All enemies defeated: combat is clearly active (they approached and were killed)
      // Assert at least one defeat occurred as proof of approach
      expect(stateLater.enemies.some((e) => e.defeatedAtTick !== null)).toBe(true)
      return
    }
    // Wait another 60 ticks for the new target
    const tickBefore = stateLater.tick
    const distBefore = Math.hypot(
      anotherAlive.x - stateLater.player.x,
      anotherAlive.y - stateLater.player.y,
    )
    await waitForTicks(page, tickBefore, 60)
    const stateEvenLater = await getGameState(page)
    const targetEvenLater = stateEvenLater.enemies.find((e) => e.id === anotherAlive.id)
    if (!targetEvenLater || targetEvenLater.defeatedAtTick !== null) {
      expect(stateEvenLater.enemies.some((e) => e.defeatedAtTick !== null)).toBe(true)
      return
    }
    const distAfter = Math.hypot(
      targetEvenLater.x - stateEvenLater.player.x,
      targetEvenLater.y - stateEvenLater.player.y,
    )
    expect(distAfter).toBeLessThan(distBefore)
    return
  }

  const dist1 = Math.hypot(enemyLater.x - stateLater.player.x, enemyLater.y - stateLater.player.y)
  expect(dist1).toBeLessThan(dist0)
})

test('GIVEN canvas pointer held WHEN ticks elapse THEN projectile appears', async ({
  page,
}) => {
  // GIVEN the player fires by holding pointer on the canvas
  // WHEN weapon cooldown elapses
  // THEN projectiles array should become non-empty (shot fired)
  //
  // Note: LoopE2ESnapshot.player does not include shotsFired; we observe
  // projectile presence as the equivalent evidence of firing activity.
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  const centerX = box!.x + box!.width / 2
  const centerY = box!.y + box!.height / 2

  await page.mouse.move(centerX, centerY)
  await page.mouse.down({ button: 'left' })

  // Wait for at least one projectile to appear (weapon interval ~280ms)
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.projectiles.length
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBeGreaterThan(0)

  await page.mouse.up()
})

test('GIVEN enemy exists WHEN projectile hits THEN enemy hp decreases or enemy defeated', async ({
  page,
}) => {
  // GIVEN an enemy has spawned and the player fires toward it
  // WHEN a projectile-enemy collision occurs
  // THEN enemy.hp should be less than maxHp OR defeatedAtTick should be set

  // Wait for enemy to appear
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.enemies.length
      },
      { timeout: 10000, intervals: [100] },
    )
    .toBeGreaterThan(0)

  const stateWithEnemy = await getGameState(page)
  const enemy0 = stateWithEnemy.enemies[0]

  // Fire toward the enemy
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  // Coordinate system: world coordinates (enemy.x/y) map to canvas CSS pixels.
  // Verify the mapping is 1:1 by comparing arena size to canvas bounding box.
  // If the ratio is not 1:1, world-to-CSS coordinate conversion is needed.
  const s = await getGameState(page)
  const scaleX = box!.width / s.arena.width
  const scaleY = box!.height / s.arena.height

  const targetX = box!.x + enemy0.x * scaleX
  const targetY = box!.y + enemy0.y * scaleY

  await page.mouse.move(targetX, targetY)
  await page.mouse.down({ button: 'left' })

  // Wait up to 8 seconds for damage to register
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        const e = s.enemies.find((en) => en.id === enemy0.id)
        if (!e) return true // enemy removed from array = defeated
        return e.hp < e.maxHp || e.defeatedAtTick !== null
      },
      { timeout: 8000, intervals: [100] },
    )
    .toBe(true)

  await page.mouse.up()
})

test('GIVEN enemy near player WHEN contact damage applies THEN player.hp decreases', async ({
  page,
}) => {
  // GIVEN an enemy spawned and approached the player close enough
  // WHEN contact damage is applied (player-enemy collision)
  // THEN player.hp should be less than player.maxHp
  //
  // Timeout is extended to 60s because the enemy needs to traverse the arena
  // to reach the player position. At typical enemy speed this can take 20-40s.
  test.setTimeout(60_000)

  const initialState = await getGameState(page)
  const initialHp = initialState.player.maxHp

  // Wait up to 50 seconds for player to take contact damage (within the 60s timeout)
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.player.hp
      },
      { timeout: 50000, intervals: [200] },
    )
    .toBeLessThan(initialHp)
})

test('GIVEN enemies field in snapshot WHEN E2E hook called THEN enemies and sortie fields present', async ({
  page,
}) => {
  // GIVEN the VITE_E2E_MODE hook is active
  // WHEN getState() is called
  // THEN the snapshot must contain enemies array and sortie object (AC5)
  const s = await getGameState(page)

  expect(Array.isArray(s.enemies)).toBe(true)
  expect(s.sortie).toBeDefined()
  expect(typeof s.sortie.status).toBe('string')
  expect(typeof s.sortie.elapsedTicks).toBe('number')
  expect(s.player.hp).toBeDefined()
  expect(s.player.maxHp).toBeDefined()
})

test('GIVEN sortie running WHEN sortie state machine checked THEN victory and defeat statuses are valid enum values', async ({
  page,
}) => {
  // GIVEN the sortie is running
  // WHEN the snapshot is inspected
  // THEN sortie.status must be one of the valid enum values including victory/defeat
  //
  // This test confirms that the SortieSystem state machine exposes the correct
  // schema for victory/defeat transitions. Full end-to-end victory (defeat all
  // enemies in 120s) and defeat (player hp → 0) cycles are not exercised in
  // automated E2E due to time constraints; see unknowns in m2-combat-mvp.md.
  const s = await getGameState(page)

  const validStatuses = ['idle', 'running', 'victory', 'defeat', 'ended']
  expect(validStatuses).toContain(s.sortie.status)

  // sortie.result is null while running, or 'victory'/'defeat' after conclusion
  if (s.sortie.result !== null) {
    expect(['victory', 'defeat']).toContain(s.sortie.result)
  }

  // Confirm the schema is structurally complete for defeat/victory outcomes
  expect(s.sortie).toMatchObject({
    status: expect.any(String),
    elapsedTicks: expect.any(Number),
    // result is null | 'victory' | 'defeat'
  })
})

test('GIVEN E2E short sortie fixture WHEN ~0.5s elapses THEN sortie.status is defeat (timeout)', async ({
  page,
}) => {
  test.setTimeout(15_000)
  // Inject short-sortie flag before page load (targetTicks ≈ 30 ticks / 0.5s)
  // With the new victory condition (allEnemiesDefeated), timer reaching targetTicks
  // results in defeat (timeout), not victory. Victory requires all enemies to be defeated.
  await page.addInitScript(() => {
    ;(window as Window & { __E2E_SHORT_SORTIE__?: boolean }).__E2E_SHORT_SORTIE__ = true
  })
  await page.goto('/')

  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 10_000, intervals: [100] },
    )
    .toBe('defeat')

  const finalState = await getGameState(page)
  expect(finalState.sortie.result).toBe('defeat')
})

test('GIVEN E2E 1HP player fixture WHEN enemy contacts player THEN sortie.status is defeat', async ({
  page,
}) => {
  test.setTimeout(30_000)
  // Inject 1HP override before page load — first enemy contact triggers defeat
  await page.addInitScript(() => {
    ;(window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }).__E2E_PLAYER_HP_OVERRIDE__ = 1
  })
  await page.goto('/')

  // Verify player starts with 1 HP
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.player.maxHp
      },
      { timeout: 5_000, intervals: [50] },
    )
    .toBe(1)

  // Wait for first enemy contact to trigger defeat
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 25_000, intervals: [200] },
    )
    .toBe('defeat')

  const finalState = await getGameState(page)
  expect(finalState.sortie.result).toBe('defeat')
})

// ---------------------------------------------------------------------------
// AC13 — Victory / Defeat HUD display (Issue #541)
// ---------------------------------------------------------------------------

test('GIVEN short sortie fixture WHEN timeout defeat THEN HUD sortie-status shows Defeat', async ({
  page,
}) => {
  // __E2E_SHORT_SORTIE__ sets targetTicks≈30 (0.5s). With the new victory condition
  // (allEnemiesDefeated), timer expiry results in defeat (timeout), not victory.
  // This test verifies the HUD updates correctly for timeout defeat.
  // NOTE: Victory HUD display requires a deterministic all-enemies-defeated fixture
  // which is tracked as a follow-up (main.ts Allowed Path not in #542 scope).
  test.setTimeout(15_000)
  await page.addInitScript(() => {
    ;(window as Window & { __E2E_SHORT_SORTIE__?: boolean }).__E2E_SHORT_SORTIE__ = true
  })
  await page.goto('/')

  // Wait for timeout defeat state machine transition
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 10_000, intervals: [100] },
    )
    .toBe('defeat')

  // HUD sortie-status DOM element must read "Defeat" (AC4, AC10)
  await expect(page.locator('[data-field="sortie-status"]')).toHaveText('Defeat', {
    timeout: 3000,
  })

  // HUD sortie-result DOM element must read "Defeat" (AC9)
  await expect(page.locator('[data-field="sortie-result"]')).toHaveText('Defeat', {
    timeout: 3000,
  })
})

test('GIVEN 1HP player fixture WHEN defeat THEN HUD sortie-status shows Defeat', async ({
  page,
}) => {
  test.setTimeout(30_000)
  await page.addInitScript(() => {
    ;(window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }).__E2E_PLAYER_HP_OVERRIDE__ = 1
  })
  await page.goto('/')

  // Wait for defeat state machine transition
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 25_000, intervals: [200] },
    )
    .toBe('defeat')

  // HUD sortie-status DOM element must read "Defeat" (AC4, AC10)
  await expect(page.locator('[data-field="sortie-status"]')).toHaveText('Defeat', {
    timeout: 3000,
  })

  // HUD sortie-result DOM element must read "Defeat" (AC9: same authority as Canvas overlay)
  await expect(page.locator('[data-field="sortie-result"]')).toHaveText('Defeat', {
    timeout: 3000,
  })
})

test('GIVEN sortie running WHEN HUD rendered THEN sortie-status shows In Progress', async ({
  page,
}) => {
  // GIVEN the sortie is in running state
  // WHEN the HUD is rendered
  // THEN data-field="sortie-status" must be "In Progress" (AC4, AC10)
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBe('running')

  await expect(page.locator('[data-field="sortie-status"]')).toHaveText('In Progress', {
    timeout: 3000,
  })
})

// ---------------------------------------------------------------------------
// Canvas bitmap visual verification (AC7, AC8) — Issue #541
// ---------------------------------------------------------------------------

test('GIVEN sortie running WHEN enemy spawns and ticks elapse THEN Canvas has enemy red pixels (enemies drawn)', async ({
  page,
}) => {
  // GIVEN enemies have spawned and at least one render frame has fired
  // WHEN we sample the Canvas bitmap for enemy pixels
  // THEN at least one red pixel (R>180, G<100, B<100) must exist
  //
  // CanvasRenderer draws alive enemies with fillStyle '#f05050' (R=240, G=80, B=80).
  // Background is '#07111f' (R=7, G=17, B=31). These are clearly distinct.
  // Scanning the full canvas avoids false negatives from enemies near the edge.
  // getImageData operates on physical pixels (canvas.width × canvas.height),
  // which already accounts for the DPR transform applied by CanvasRenderer.

  // Wait for sortie to be running
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBe('running')

  // Wait for at least one enemy to spawn so the canvas has enemy circles
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.enemies.length
      },
      { timeout: 10_000, intervals: [100] },
    )
    .toBeGreaterThan(0)

  // Allow a few extra render frames to propagate
  const stateAfterEnemy = await getGameState(page)
  await waitForTicks(page, stateAfterEnemy.tick, 5)

  // Verify canvas has enemy-red pixels: R>180 AND G<100 AND B<100 (AC7)
  // This distinguishes '#f05050' enemy circles from the '#07111f' background.
  const hasEnemyRedPixels = await page.evaluate(() => {
    const canvas = document.querySelector('canvas') as HTMLCanvasElement | null
    if (!canvas) return false
    const ctx = canvas.getContext('2d')
    if (!ctx) return false
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
    for (let i = 0; i < data.length; i += 4) {
      if (data[i] > 180 && data[i + 1] < 100 && data[i + 2] < 100) {
        return true
      }
    }
    return false
  })
  expect(hasEnemyRedPixels).toBe(true)
})

test('GIVEN short sortie fixture WHEN timeout defeat THEN Canvas overlay has red-dominant pixels (defeat overlay drawn)', async ({
  page,
}) => {
  // GIVEN the sortie reaches timeout defeat via __E2E_SHORT_SORTIE__ fixture (targetTicks≈30)
  // WHEN the CanvasRenderer paints the defeat overlay (AC8)
  // THEN the canvas center region must contain red-dominant pixels
  //
  // NOTE: __E2E_SHORT_SORTIE__ now triggers timeout→defeat (not victory).
  // Victory overlay (green) is not testable via E2E until a deterministic
  // all-enemies-defeated fixture is added (follow-up, main.ts not in #542 scope).
  //
  // CanvasRenderer defeat overlay: 'rgba(220, 60, 60, 0.55)' blended over '#07111f'.
  // Blend result approximation: R≈124, G≈41, B≈41.
  // R>80 AND G<60 distinguishes defeat overlay from background and victory overlay.
  test.setTimeout(15_000)
  await page.addInitScript(() => {
    ;(window as Window & { __E2E_SHORT_SORTIE__?: boolean }).__E2E_SHORT_SORTIE__ = true
  })
  await page.goto('/')

  // Wait for timeout defeat transition
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 10_000, intervals: [100] },
    )
    .toBe('defeat')

  // Allow a couple of render frames to ensure the overlay is painted.
  await page.waitForTimeout(200)

  // Canvas center region must have red-dominant pixels (R>80 AND G<60) from defeat overlay (AC8)
  const hasDefeatRedPixels = await page.evaluate(() => {
    const canvas = document.querySelector('canvas') as HTMLCanvasElement | null
    if (!canvas) return false
    const ctx = canvas.getContext('2d')
    if (!ctx) return false
    const w = canvas.width
    const h = canvas.height
    const x0 = Math.floor(w / 4)
    const y0 = Math.floor(h / 4)
    const scanW = Math.floor(w / 2)
    const scanH = Math.floor(h / 2)
    const { data } = ctx.getImageData(x0, y0, scanW, scanH)
    for (let i = 0; i < data.length; i += 4) {
      // Defeat overlay blends to R≈124, G≈41; background R=7, G=17
      if (data[i] > 80 && data[i + 1] < 60) {
        return true
      }
    }
    return false
  })
  expect(hasDefeatRedPixels).toBe(true)

  // Additionally confirm the HUD defeat label is visible (belt-and-suspenders)
  await expect(page.locator('[data-field="sortie-result"]')).toHaveText('Defeat', {
    timeout: 3000,
  })
})

test('GIVEN 1HP player fixture WHEN defeat THEN Canvas overlay has red-dominant pixels (defeat overlay drawn)', async ({
  page,
}) => {
  // GIVEN the player starts with 1HP so first enemy contact triggers defeat
  // WHEN the CanvasRenderer paints the defeat overlay (AC8)
  // THEN the canvas center region must contain red-dominant pixels
  //
  // CanvasRenderer defeat overlay: 'rgba(220, 60, 60, 0.55)' blended over '#07111f'.
  // Blend result approximation: R≈124, G≈41, B≈41.
  // R>80 AND G<60 distinguishes defeat overlay from background (R=7,G=17) and
  // from victory overlay (G≈118). Enemy red (#f05050, R=240) is gone after defeat.
  test.setTimeout(30_000)
  await page.addInitScript(() => {
    ;(window as Window & { __E2E_PLAYER_HP_OVERRIDE__?: number }).__E2E_PLAYER_HP_OVERRIDE__ = 1
  })
  await page.goto('/')

  // Wait for defeat transition
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.sortie.status
      },
      { timeout: 25_000, intervals: [200] },
    )
    .toBe('defeat')

  // Allow a couple of render frames to ensure the overlay is painted.
  // waitForTicks is not used here because the simulation loop may pause after
  // the sortie reaches a terminal state; a short fixed wait is sufficient.
  await page.waitForTimeout(200)

  // Canvas center region must have red-dominant pixels (R>80 AND G<60) from defeat overlay (AC8)
  const hasDefeatRedPixels = await page.evaluate(() => {
    const canvas = document.querySelector('canvas') as HTMLCanvasElement | null
    if (!canvas) return false
    const ctx = canvas.getContext('2d')
    if (!ctx) return false
    const w = canvas.width
    const h = canvas.height
    const x0 = Math.floor(w / 4)
    const y0 = Math.floor(h / 4)
    const scanW = Math.floor(w / 2)
    const scanH = Math.floor(h / 2)
    const { data } = ctx.getImageData(x0, y0, scanW, scanH)
    for (let i = 0; i < data.length; i += 4) {
      // Defeat overlay blends to R≈124, G≈41; background R=7,G=17; threshold R>80,G<60 is safe
      if (data[i] > 80 && data[i + 1] < 60) {
        return true
      }
    }
    return false
  })
  expect(hasDefeatRedPixels).toBe(true)

  // Additionally confirm the HUD defeat label is visible (belt-and-suspenders)
  await expect(page.locator('[data-field="sortie-result"]')).toHaveText('Defeat', {
    timeout: 3000,
  })
})

// ---------------------------------------------------------------------------
// AC5 (Issue #581) -- HP label bounds verification
// runtime_verification: true -- Playwright chromium headless
// ---------------------------------------------------------------------------

test(
  `GIVEN max HP enemy spawned WHEN rendered THEN HP label bounding box is within arena bounds`,
  async ({ page }) => {
    // GIVEN the sortie is running and at least one enemy has spawned
    // WHEN the CanvasRenderer draws the HP label (drawEnemyHpLabel)
    // THEN the label bounding box is fully within arena bounds (no overflow)
    //
    // Verification method:
    //   1. Read arena dimensions from __LOOP_E2E__ hook
    //   2. Sample the canvas border pixels (1-pixel edge stripe)
    //   3. Assert no white (HP label) pixels exist in the border stripe
    //      White = R>200 AND G>200 AND B>200 (HP_LABEL_COLOR #ffffff)
    //      Background = #07111f (R=7, G=17, B=31) -- clearly distinct
    //
    // Note: this test verifies label pixels do not reach the canvas edge.
    // The padding=2 clamp ensures labels stay >= 2px from the edge.
    // Scanning the 1px border (innermost physical pixel row/col) detects overflow.

    test.setTimeout(20_000)

    // Wait for sortie running
    await expect
      .poll(
        async () => {
          const s = await getGameState(page)
          return s.sortie.status
        },
        { timeout: 5000, intervals: [50] },
      )
      .toBe(`running`)

    // Wait for at least one enemy to spawn
    await expect
      .poll(
        async () => {
          const s = await getGameState(page)
          return s.enemies.length
        },
        { timeout: 15000, intervals: [100] },
      )
      .toBeGreaterThan(0)

    // Allow a few extra render frames
    const stateAfter = await getGameState(page)
    await waitForTicks(page, stateAfter.tick, 5)

    // Check HP label bounds: scan 1px border stripe for white pixels
    const hpLabelInBounds = await page.evaluate(() => {
      const canvas = document.querySelector(`canvas`) as HTMLCanvasElement | null
      if (!canvas) return { ok: false, reason: `no canvas` }
      const ctx = canvas.getContext(`2d`)
      if (!ctx) return { ok: false, reason: `no ctx` }
      const w = canvas.width
      const h = canvas.height
      if (w === 0 || h === 0) return { ok: false, reason: `zero dims` }

      // Scan the outermost 1px border for white (label) pixels
      // HP_LABEL_COLOR is #ffffff: R=255, G=255, B=255
      // Background #07111f: R=7, G=17, B=31 -- distinctly non-white
      // Threshold: R>200 AND G>200 AND B>200
      function isWhite(r: number, g: number, b: number): boolean {
        return r > 200 && g > 200 && b > 200
      }

      // Top row
      const topRow = ctx.getImageData(0, 0, w, 1).data
      for (let i = 0; i < topRow.length; i += 4) {
        if (isWhite(topRow[i], topRow[i + 1], topRow[i + 2])) {
          return { ok: false, reason: `white pixel in top border` }
        }
      }
      // Bottom row
      const botRow = ctx.getImageData(0, h - 1, w, 1).data
      for (let i = 0; i < botRow.length; i += 4) {
        if (isWhite(botRow[i], botRow[i + 1], botRow[i + 2])) {
          return { ok: false, reason: `white pixel in bottom border` }
        }
      }
      // Left column
      const leftCol = ctx.getImageData(0, 0, 1, h).data
      for (let i = 0; i < leftCol.length; i += 4) {
        if (isWhite(leftCol[i], leftCol[i + 1], leftCol[i + 2])) {
          return { ok: false, reason: `white pixel in left border` }
        }
      }
      // Right column
      const rightCol = ctx.getImageData(w - 1, 0, 1, h).data
      for (let i = 0; i < rightCol.length; i += 4) {
        if (isWhite(rightCol[i], rightCol[i + 1], rightCol[i + 2])) {
          return { ok: false, reason: `white pixel in right border` }
        }
      }
      return { ok: true, reason: `no label pixels in border stripe` }
    })

    expect(hpLabelInBounds.ok, hpLabelInBounds.reason).toBe(true)
  },
)