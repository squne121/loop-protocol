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
 * AC7: the write-set operation-history audit records zero setItem /
 *      removeItem / clear operations against the production key across
 *      the entire scenario (including reloads), and the production
 *      sentinel is initialized exactly once at context level (never via
 *      an addInitScript that reruns per navigation).
 * AC8: the full write-set operation history (setItem/removeItem/clear,
 *      key, order), preserved across reload, contains zero operations
 *      outside the allowed E2E runtime key — including an intermediate
 *      clear() followed by regeneration under a different key. The final
 *      localStorage key set alone is not sufficient evidence for AC8.
 * AC10: `__LOOP_E2E__` is read-only — it exposes `getState()` only, and the
 *       returned snapshot never carries mutation methods.
 *
 * 2026-08-03 OWNER review repair (issuecomment-5165010968): the prior
 * `installBootstrapFixture()` used `page.addInitScript()` to
 * unconditionally `setItem` the production sentinel, which reruns on
 * EVERY navigation/reload — if the app under test ever mutated the
 * production key mid-scenario, the next reload would silently restore the
 * sentinel and mask the regression (false-green). This version seeds the
 * production sentinel and the E2E key exactly once, at context creation,
 * via `browser.newContext({ storageState })` (Playwright applies
 * `storageState` before any user-registered `context.addInitScript()`
 * runs, so it is never re-applied on reload). A Node-side write-set
 * operation-history audit (`context.exposeBinding()` + a
 * `context.addInitScript()` that monkey-patches
 * `Storage.prototype.{setItem,removeItem,clear}`) records every write
 * across the full scenario, including reloads, so AC7/AC8 can assert
 * "zero operations", not just "final key set matches".
 */

import { test, expect, type Page, type BrowserContext, type Browser } from '@playwright/test'
// Type-only import from the single source of truth (Issue #1283 PR #1517
// review fix): avoids a locally-duplicated snapshot type that can silently
// drift from the real `__LOOP_E2E__.getState()` return shape.
import type { LoopE2ESnapshot } from '../../src/main'

// ---------------------------------------------------------------------------
// Helper: read-only __LOOP_E2E__ observability hook
// ---------------------------------------------------------------------------

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
    return hook.getState()
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

// ---------------------------------------------------------------------------
// Write-set operation-history audit (AC7 / AC8, Design Constraints)
// ---------------------------------------------------------------------------

type WriteOp = {
  type: 'setItem' | 'removeItem' | 'clear'
  key: string | null
  order: number
}

/**
 * Creates a browser context whose production sentinel and E2E seed are
 * initialized exactly once, at context creation, via
 * `browser.newContext({ storageState })` (never via `addInitScript`, which
 * reruns on every navigation/reload — the exact false-green mechanism this
 * repair fixes). Also installs the Node-side write-set operation-history
 * audit: a monkey-patched `Storage.prototype` (installed via
 * `context.addInitScript()`, which must run on every navigation so the
 * audit survives reload) forwards every `setItem`/`removeItem`/`clear`
 * call to an `exposeBinding()`-backed in-memory log kept in this test
 * process (Design Constraints: "write log ... Node 側メモリに蓄積し、
 * navigation 後も保持する").
 */
async function createAuditedContext(
  browser: Browser,
  baseURL: string,
  seed: { productionKey: string; productionSentinel: string; e2eKey: string; e2eSeed: string },
): Promise<{ context: BrowserContext; page: Page; writeLog: WriteOp[] }> {
  const writeLog: WriteOp[] = []

  const context = await browser.newContext({
    baseURL,
    storageState: {
      cookies: [],
      origins: [
        {
          origin: baseURL,
          localStorage: [
            { name: seed.productionKey, value: seed.productionSentinel },
            { name: seed.e2eKey, value: seed.e2eSeed },
          ],
        },
      ],
    },
  })

  // Node-side sink for the operation-history audit. Registered before the
  // patch-installing addInitScript below so it is available to every page
  // load in this context (including the very first navigation).
  await context.exposeBinding(
    '__loopE2EWriteLog',
    (_source: unknown, op: { type: 'setItem' | 'removeItem' | 'clear'; key: string | null; order: number }) => {
      writeLog.push(op)
    },
  )

  // Runtime-only config (JS globals, not storage) + the localStorage
  // write-set instrumentation. Safe to reinstall on every navigation/reload:
  // unlike the prior sentinel-writing addInitScript, this patch itself never
  // writes to storage — it only observes writes the app under test makes.
  await context.addInitScript(
    (init: { e2eKey: string }) => {
      ;(window as Window & { __LOOP_STORAGE_KEY__?: string }).__LOOP_STORAGE_KEY__ = init.e2eKey
      ;(
        window as Window & { __LOOP_E2E_BOOTSTRAP__?: { autoStart?: boolean } }
      ).__LOOP_E2E_BOOTSTRAP__ = { autoStart: false }

      let order = 0
      const w = window as Window & { __loopE2EWriteLog?: (op: unknown) => void }
      const originalSetItem = Storage.prototype.setItem
      const originalRemoveItem = Storage.prototype.removeItem
      const originalClear = Storage.prototype.clear

      Storage.prototype.setItem = function patchedSetItem(key: string, value: string) {
        if (this === window.localStorage && w.__loopE2EWriteLog) {
          w.__loopE2EWriteLog({ type: 'setItem', key, order: order++ })
        }
        return originalSetItem.call(this, key, value)
      }
      Storage.prototype.removeItem = function patchedRemoveItem(key: string) {
        if (this === window.localStorage && w.__loopE2EWriteLog) {
          w.__loopE2EWriteLog({ type: 'removeItem', key, order: order++ })
        }
        return originalRemoveItem.call(this, key)
      }
      Storage.prototype.clear = function patchedClear() {
        if (this === window.localStorage && w.__loopE2EWriteLog) {
          w.__loopE2EWriteLog({ type: 'clear', key: null, order: order++ })
        }
        return originalClear.call(this)
      }
    },
    { e2eKey: seed.e2eKey },
  )

  const page = await context.newPage()
  return { context, page, writeLog }
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

test(
  'M4 upgrade loop: GIVEN seeded snapshot WHEN Load Game -> Upgrade weapon -> reload -> Load Game -> Launch sortie -> fire THEN persistence and projectile damage reflect the purchased weaponPower, and the write-set operation history proves zero production-key / out-of-scope mutations',
  async ({ browser }, testInfo) => {
    test.setTimeout(60_000)

    const e2eKey = buildE2EStorageKey(testInfo)
    const e2eSeed = JSON.stringify({
      schemaVersion: 1,
      resources: 100,
      weaponPower: 1,
      playerMaxHp: 8,
    })
    const baseURL = (testInfo.project.use.baseURL as string | undefined) ?? 'http://127.0.0.1:4173'

    const { context, page, writeLog } = await createAuditedContext(browser, baseURL, {
      productionKey: PRODUCTION_KEY,
      productionSentinel: PRODUCTION_SENTINEL,
      e2eKey,
      e2eSeed,
    })

    try {
      // './' (not '/') — a leading '/' is an absolute path that REPLACES the
      // baseURL's path segment entirely (new URL('/', base).pathname === '/'),
      // which would silently navigate to root and make the nested-base
      // assertions below vacuous when baseURL has a non-root VITE_BASE_PATH
      // (Issue #1283 AC9 repair).
      await page.goto('./')

      // AC10: __LOOP_E2E__ is read-only — getState() only, no mutation methods.
      // Reflect.ownKeys() (rather than Object.keys()) also surfaces
      // non-enumerable own properties, so a mutation method hidden via
      // Object.defineProperty(..., { enumerable: false }) cannot slip past
      // this check (PR #1517 review fix).
      const hookShape = await page.evaluate(() => {
        const hook = (window as Window & { __LOOP_E2E__?: object }).__LOOP_E2E__
        if (!hook) return null
        return Reflect.ownKeys(hook).map(String)
      })
      expect(hookShape, '__LOOP_E2E__ must exist (AC10)').not.toBeNull()
      expect(
        hookShape,
        '__LOOP_E2E__ must expose only getState() via Reflect.ownKeys (AC10)',
      ).toEqual(['getState'])

      // AC10: getState() must return a fresh, isolated snapshot on every call —
      // mutating a previously-returned snapshot must not leak into a later
      // getState() call (proves it is not a live-state reference).
      const snapshotIsolation = await page.evaluate(() => {
        const hook = (
          window as Window & { __LOOP_E2E__?: { getState: () => { projectiles: unknown[] } } }
        ).__LOOP_E2E__!
        const first = hook.getState()
        const originalLength = first.projectiles.length
        first.projectiles.push({ injected: true })
        const second = hook.getState()
        return {
          originalLength,
          mutatedFirstLength: first.projectiles.length,
          secondLength: second.projectiles.length,
        }
      })
      expect(
        snapshotIsolation.mutatedFirstLength,
        'sanity: the local mutation itself must have applied (AC10 test harness check)',
      ).toBe(snapshotIsolation.originalLength + 1)
      expect(
        snapshotIsolation.secondLength,
        'mutating a returned snapshot must not leak into subsequent getState() calls (AC10)',
      ).toBe(snapshotIsolation.originalLength)

      // AC1 / bootstrap: autoStart disabled — app stays at title_menu, not auto-advanced.
      const initialState = await getGameState(page)
      expect(initialState.loopPhase, 'app must stay at title_menu when autoStart is disabled').toBe(
        'title_menu',
      )

      // AC1: the E2E key must already hold the seeded snapshot at first
      // navigation (context-level storageState, not an app write).
      const seededAtStart = await readStorageKey(page, e2eKey)
      expect(seededAtStart, 'E2E key must be pre-seeded via context storageState (AC1)').not.toBeNull()
      expect(seededAtStart!.resources, 'seeded resources must be 100 (AC1)').toBe(100)
      expect(seededAtStart!.weaponPower, 'seeded weaponPower must be 1 (AC1)').toBe(1)
      expect(seededAtStart!.playerMaxHp, 'seeded playerMaxHp must be 8 (AC1)').toBe(8)

      // AC2: title menu -> Load Game (Issue #1374 PR #1815 review: navigation
      // (open-load-menu-title, into load_menu) and the actual load
      // (confirm-load, only reachable from load_menu) are now separate
      // intent-only controls -- main.ts owns the transition via
      // transitionByIntent(), not a single dual-purpose button).
      const openLoadMenuButton = page.locator('[data-action="open-load-menu-title"]')
      await expect(openLoadMenuButton).toBeEnabled({ timeout: 5_000 })
      await openLoadMenuButton.click()
      await expect(page.locator('[data-field="status"]')).toHaveText('Load Menu.', {
        timeout: 5_000,
      })
      const confirmLoadButton = page.locator('[data-action="confirm-load"]')
      await expect(confirmLoadButton).toBeEnabled({ timeout: 5_000 })
      await confirmLoadButton.click()
      await expect(page.locator('[data-field="status"]')).toHaveText('Load Game complete.', {
        timeout: 5_000,
      })

      // AC2: HUD shows the seeded snapshot (preparation screen fields).
      await expect(page.locator('[data-field="prep-resources"]')).toHaveText('100', { timeout: 5_000 })
      await expect(page.locator('[data-field="prep-weapon-power"]')).toHaveText('1', { timeout: 5_000 })

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

      await expect(page.locator('[data-field="prep-resources"]')).toHaveText('0', { timeout: 5_000 })
      await expect(page.locator('[data-field="prep-weapon-power"]')).toHaveText('2', { timeout: 5_000 })

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

      // AC4 / AC7: reload — the audit-installing addInitScript re-runs (it
      // must, to keep observing writes), but it performs no storage writes
      // itself, and the production sentinel / E2E seed are NOT re-applied
      // (they were context-level storageState, applied exactly once).
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
      const openLoadMenuButtonAfterReload = page.locator('[data-action="open-load-menu-title"]')
      await expect(openLoadMenuButtonAfterReload).toBeEnabled({ timeout: 5_000 })
      await openLoadMenuButtonAfterReload.click()
      await expect(page.locator('[data-field="status"]')).toHaveText('Load Menu.', {
        timeout: 5_000,
      })
      const confirmLoadButtonAfterReload = page.locator('[data-action="confirm-load"]')
      await expect(confirmLoadButtonAfterReload).toBeEnabled({ timeout: 5_000 })
      await confirmLoadButtonAfterReload.click()
      await expect(page.locator('[data-field="status"]')).toHaveText('Load Game complete.', {
        timeout: 5_000,
      })

      await expect(page.locator('[data-field="prep-weapon-power"]')).toHaveText('2', { timeout: 5_000 })

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

      // AC6 (PR #1517 review fix): identify the newly-fired projectile by ID
      // diff against the pre-fire set, rather than assuming index 0 — the
      // running phase may already contain projectiles from a prior frame, and
      // indexing projectiles[0] would silently pass against a stale/unrelated
      // entry instead of the projectile this fire actually created.
      const beforeFire = await getGameState(page)
      const existingProjectileIds = new Set(beforeFire.projectiles.map((p) => p.id))

      let newProjectile: LoopE2ESnapshot['projectiles'][number] | null = null
      await page.mouse.move(centerX, centerY)
      await page.mouse.down({ button: 'left' })
      try {
        await expect
          .poll(
            async () => {
              const current = await getGameState(page)
              newProjectile = current.projectiles.find((p) => !existingProjectileIds.has(p.id)) ?? null
              return newProjectile
            },
            { timeout: 3_000, intervals: [50] },
          )
          .not.toBeNull()
      } finally {
        await page.mouse.up({ button: 'left' })
      }

      expect(newProjectile, 'a newly-fired projectile must have appeared (AC6)').not.toBeNull()
      expect(
        newProjectile!.damage,
        'newly fired projectile damage must equal the restored weaponPower (AC6)',
      ).toBe(beforeFire.progress.weaponPower)

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
      // scenario touches; no third key was created by the app. Retained as a
      // sanity check in addition to (not instead of) the write-set audit
      // below, per the Issue's explicit "final key set alone is not
      // sufficient" instruction.
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
        'only the production sentinel and the E2E-scoped key must exist (AC8 sanity check)',
      ).toEqual([PRODUCTION_KEY, e2eKey].sort())

      // AC7: the write-set operation-history audit records zero
      // setItem/removeItem/clear operations against the production key
      // across the entire scenario (including the reload).
      const productionKeyOps = writeLog.filter((op) => op.key === PRODUCTION_KEY)
      expect(
        productionKeyOps,
        'write-set operation history must record zero operations against the production key (AC7)',
      ).toEqual([])

      // AC7: production sentinel initialization must not have been observed
      // as an app-triggered write at all (it was applied via context
      // storageState before the audit patch could observe it) — the audit
      // log must contain no setItem for the production key even at time 0.
      expect(
        writeLog.some((op) => op.type === 'setItem' && op.key === PRODUCTION_KEY),
        'production sentinel must never be (re-)written by the app via setItem (AC7)',
      ).toBe(false)

      // AC8: zero clear() calls (clear() implicitly operates on every key,
      // including out-of-scope ones, so any clear() call is itself an
      // out-of-scope operation regardless of which key a subsequent
      // regeneration targets).
      const clearOps = writeLog.filter((op) => op.type === 'clear')
      expect(
        clearOps,
        'write-set operation history must record zero clear() calls (AC8: an intermediate ' +
          'clear() followed by regeneration is still an out-of-scope operation)',
      ).toEqual([])

      // AC8: every non-clear write-set operation must target only the
      // allowed E2E runtime key — the full operation history, not just the
      // final key set, must contain zero operations elsewhere.
      const outOfScopeOps = writeLog.filter((op) => op.type !== 'clear' && op.key !== e2eKey)
      expect(
        outOfScopeOps,
        'write-set operation history must record zero operations outside the allowed E2E ' +
          'runtime key (AC8)',
      ).toEqual([])

      // AC8: at least one in-scope write must have been observed (sanity —
      // proves the audit instrumentation actually captured the purchase
      // save, not merely that nothing happened).
      const inScopeOps = writeLog.filter((op) => op.type !== 'clear' && op.key === e2eKey)
      expect(
        inScopeOps.length,
        'write-set operation history must have captured at least the post-purchase save ' +
          '(AC8 harness sanity check)',
      ).toBeGreaterThan(0)
    } finally {
      await context.close()
    }
  },
)
