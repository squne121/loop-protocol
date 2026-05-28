/**
 * E2E smoke test: movement + projectile foundation (Issue #447)
 *
 * Covers AC5 behavioral assertions:
 * - Canvas is visible with non-zero CSS size
 * - KeyW/KeyA/KeyS/KeyD → player position changes on expected axis
 * - keyup stops movement input
 * - canvas pointer down → projectile generated (count > 0)
 * - simulation tick advances projectile count / position
 * - pointerup clears primary fire input
 * - pointercancel / lostpointercapture clears active pointer state
 *
 * NOTE: Automatic E2E results are NOT UX validity evidence.
 * They confirm integration correctness only (AC10).
 * Human playtesting remains required to evaluate UX.
 */

import { test, expect, type Page } from '@playwright/test'

// ---------------------------------------------------------------------------
// Helper: access the read-only __LOOP_E2E__ observability hook exposed only
// when VITE_E2E_MODE=true (AC12: absent in production builds).
// ---------------------------------------------------------------------------

interface LoopE2EState {
  tick: number
  elapsedMs: number
  player: {
    x: number
    y: number
    aimX: number
    aimY: number
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

/** Wait for at least one simulation tick to advance. */
async function waitForTick(page: Page, fromTick: number): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.tick
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBeGreaterThan(fromTick)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.beforeEach(async ({ page }) => {
  await page.goto('/')
})

test('canvas is visible with non-zero CSS size', async ({ page }) => {
  // GIVEN the app has loaded
  // WHEN we query the battle stage canvas
  // THEN it should be visible with non-zero dimensions
  const canvas = page.locator('canvas.battle-stage__canvas')
  await expect(canvas).toBeVisible()

  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.width).toBeGreaterThan(0)
  expect(box!.height).toBeGreaterThan(0)
})

test('simulation loop is running (tick advances)', async ({ page }) => {
  // GIVEN the app is loaded
  // WHEN we wait briefly
  // THEN the simulation tick counter should have advanced
  const initialState = await getGameState(page)
  await waitForTick(page, initialState.tick)
  const laterState = await getGameState(page)
  expect(laterState.tick).toBeGreaterThan(initialState.tick)
})

test('KeyW moves player upward (y decreases)', async ({ page }) => {
  // GIVEN the player is at initial position
  // WHEN KeyW is held for several frames
  // THEN player.y should decrease (upward movement)
  const before = await getGameState(page)
  const tickBefore = before.tick
  const yBefore = before.player.y

  await page.keyboard.down('KeyW')
  // Wait for enough ticks to see movement
  await waitForTick(page, tickBefore + 5)
  await page.keyboard.up('KeyW')

  const after = await getGameState(page)
  expect(after.player.y).toBeLessThan(yBefore)
})

test('KeyS moves player downward (y increases)', async ({ page }) => {
  // GIVEN the player is at initial position
  // WHEN KeyS is held for several frames
  // THEN player.y should increase (downward movement)
  const before = await getGameState(page)
  const tickBefore = before.tick
  const yBefore = before.player.y

  await page.keyboard.down('KeyS')
  await waitForTick(page, tickBefore + 5)
  await page.keyboard.up('KeyS')

  const after = await getGameState(page)
  expect(after.player.y).toBeGreaterThan(yBefore)
})

test('KeyA moves player left (x decreases)', async ({ page }) => {
  // GIVEN the player is at initial position
  // WHEN KeyA is held for several frames
  // THEN player.x should decrease (leftward movement)
  const before = await getGameState(page)
  const tickBefore = before.tick
  const xBefore = before.player.x

  await page.keyboard.down('KeyA')
  await waitForTick(page, tickBefore + 5)
  await page.keyboard.up('KeyA')

  const after = await getGameState(page)
  expect(after.player.x).toBeLessThan(xBefore)
})

test('KeyD moves player right (x increases)', async ({ page }) => {
  // GIVEN the player is at initial position
  // WHEN KeyD is held for several frames
  // THEN player.x should increase (rightward movement)
  const before = await getGameState(page)
  const tickBefore = before.tick
  const xBefore = before.player.x

  await page.keyboard.down('KeyD')
  await waitForTick(page, tickBefore + 5)
  await page.keyboard.up('KeyD')

  const after = await getGameState(page)
  expect(after.player.x).toBeGreaterThan(xBefore)
})

test('keyup stops movement — player position stabilises after key release', async ({
  page,
}) => {
  // GIVEN the player has moved with KeyD
  // WHEN KeyD is released and we wait several more ticks
  // THEN player.x should not change after key release
  const initial = await getGameState(page)
  const tickStart = initial.tick

  await page.keyboard.down('KeyD')
  await waitForTick(page, tickStart + 5)
  await page.keyboard.up('KeyD')

  // Record position right after keyup
  const afterRelease = await getGameState(page)
  const xAfterRelease = afterRelease.player.x
  const tickAfterRelease = afterRelease.tick

  // Wait for more ticks with no key held
  await waitForTick(page, tickAfterRelease + 5)
  const afterWait = await getGameState(page)

  // Position should not have changed (no movement command active)
  expect(afterWait.player.x).toBeCloseTo(xAfterRelease, 0)
})

test('pointer down on canvas generates at least one projectile', async ({
  page,
}) => {
  // GIVEN the simulation is running with no projectiles
  // WHEN we simulate a pointer down on the canvas center
  // THEN at least one projectile should appear within several frames
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  const centerX = box!.x + box!.width / 2
  const centerY = box!.y + box!.height / 2

  const stateBefore = await getGameState(page)
  const tickBefore = stateBefore.tick

  // Move to canvas center FIRST so pointerdown fires on the canvas element
  await page.mouse.move(centerX, centerY)
  await page.mouse.down({ button: 'left' })

  // Wait for combat system to fire (weapon interval is 280ms, wait up to 3s)
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

  // Ensure tick actually advanced
  const stateAfter = await getGameState(page)
  expect(stateAfter.tick).toBeGreaterThan(tickBefore)
})

test('projectile position changes after simulation ticks (projectile moves)', async ({
  page,
}) => {
  // GIVEN a projectile has been fired
  // WHEN several simulation ticks elapse
  // THEN the projectile position should have changed
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  const centerX = box!.x + box!.width / 2
  const centerY = box!.y + box!.height / 2

  // Fire a projectile
  await page.mouse.move(centerX, centerY)
  await page.mouse.down({ button: 'left' })

  // Wait for a projectile to appear
  await expect
    .poll(
      async () => {
        const s = await getGameState(page)
        return s.projectiles.length
      },
      { timeout: 3000, intervals: [50] },
    )
    .toBeGreaterThan(0)

  // Record projectile position
  const stateWithProjectile = await getGameState(page)
  const firstProjectile = stateWithProjectile.projectiles[0]

  await page.mouse.up()

  // Assert the specific projectile moves within 1 second.
  // The arena is large enough that a freshly-fired projectile cannot leave bounds
  // within 1 second (weapon fires from center, arena is 800×600 minimum).
  await expect
    .poll(
      async () => {
        const s = await page.evaluate(() =>
          (
            window as Window & {
              __LOOP_E2E__?: { getState: () => LoopE2EState }
            }
          ).__LOOP_E2E__!.getState(),
        )
        const p = s.projectiles.find((c) => c.id === firstProjectile.id)
        if (!p) return false
        return p.x !== firstProjectile.x || p.y !== firstProjectile.y
      },
      { timeout: 1000, intervals: [16, 33, 50] },
    )
    .toBe(true)
})

test('pointerup clears primary fire state', async ({ page }) => {
  // GIVEN pointer is down (fire active)
  // WHEN pointerup is dispatched
  // THEN no new projectiles should be generated after several frames
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  const cx = box!.x + box!.width / 2
  const cy = box!.y + box!.height / 2

  await page.mouse.move(cx, cy)
  await page.mouse.down({ button: 'left' })

  // Wait for first fire
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

  // Assert pointer state cleared immediately after pointerup
  const afterUp = await getGameState(page)
  expect(afterUp.input.primaryPressed).toBe(false)
  expect(afterUp.input.activePointerId).toBeNull()

  // Also verify simulation keeps running
  const tickAfterUp = afterUp.tick
  await waitForTick(page, tickAfterUp + 10)

  const stateFinal = await getGameState(page)
  expect(stateFinal.tick).toBeGreaterThan(tickAfterUp)
})

test('pointercancel clears active pointer state', async ({ page }) => {
  // GIVEN a pointerdown has been dispatched to the canvas
  // WHEN pointercancel is dispatched on the canvas
  // THEN primaryPressed should become false (no further projectiles fired)
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  const cx = box!.x + box!.width / 2
  const cy = box!.y + box!.height / 2

  // Use dispatchEvent to simulate pointerdown then pointercancel
  await page.mouse.move(cx, cy)
  await page.mouse.down({ button: 'left' })

  // Wait briefly so fire starts
  const before = await getGameState(page)
  await waitForTick(page, before.tick + 3)

  // Dispatch pointercancel via JS to canvas
  await canvas.dispatchEvent('pointercancel', {
    bubbles: true,
    cancelable: true,
    pointerId: 1,
    isPrimary: true,
    button: 0,
  })

  // Assert pointer state cleared after pointercancel
  const afterCancel = await getGameState(page)
  expect(afterCancel.input.primaryPressed).toBe(false)
  expect(afterCancel.input.activePointerId).toBeNull()

  // Also verify simulation keeps running
  const tickAfterCancel = afterCancel.tick
  await waitForTick(page, tickAfterCancel + 10)

  const stateFinal = await getGameState(page)
  expect(stateFinal.tick).toBeGreaterThan(tickAfterCancel)
})

test('lostpointercapture clears active pointer state', async ({ page }) => {
  // GIVEN a pointerdown has been dispatched to the canvas
  // WHEN lostpointercapture is dispatched on the canvas
  // THEN primaryPressed should become false and activePointerId should be null
  const canvas = page.locator('canvas.battle-stage__canvas')

  await canvas.dispatchEvent('pointerdown', {
    bubbles: true,
    cancelable: true,
    pointerId: 1,
    isPrimary: true,
    button: 0,
  })
  await page.waitForTimeout(100)

  const before = await getGameState(page)
  // pointerdown should have activated primary fire
  expect(before.input.primaryPressed).toBe(true)
  expect(before.input.activePointerId).toBe(1)

  // Dispatch lostpointercapture to simulate pointer capture being lost
  await canvas.dispatchEvent('lostpointercapture', {
    bubbles: false,
    cancelable: false,
    pointerId: 1,
  })
  await page.waitForTimeout(100)

  const after = await getGameState(page)
  expect(after.input.primaryPressed).toBe(false)
  expect(after.input.activePointerId).toBeNull()
})

test('projectile renders on canvas', async ({ page }) => {
  // GIVEN the app is loaded and the canvas is idle
  // WHEN a pointer down is held long enough to fire a projectile
  // THEN the canvas pixel data should differ from the pre-fire state
  const canvas = page.locator('canvas.battle-stage__canvas')
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()

  const centerX = box!.x + box!.width / 2
  const centerY = box!.y + box!.height / 2

  // Wait for initial render to settle
  await page.waitForTimeout(100)

  // Record canvas before firing
  const beforeDataUrl = await page.evaluate(() => {
    const c = document.querySelector('canvas') as HTMLCanvasElement
    return c.toDataURL()
  })

  // Fire projectile
  await page.mouse.move(centerX, centerY)
  await page.mouse.down({ button: 'left' })

  // Wait for weapon interval (280ms) plus render frame buffer
  await page.waitForTimeout(400)

  await page.mouse.up()

  // Record canvas after firing
  const afterDataUrl = await page.evaluate(() => {
    const c = document.querySelector('canvas') as HTMLCanvasElement
    return c.toDataURL()
  })

  // Canvas pixel data must have changed (projectile rendered)
  expect(afterDataUrl).not.toBe(beforeDataUrl)
})
