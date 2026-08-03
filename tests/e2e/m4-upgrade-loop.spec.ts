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
 * runs, so it is never re-applied on reload).
 *
 * 2026-08-03 OWNER REQUEST_CHANGES repair, iteration 1
 * (issuecomment-5167160762 on PR #1981): the write-set operation-history
 * audit itself had three unresolved false-green paths, all fixed here:
 *
 * - P1 Blocker 1 (order resets per reload): the prior implementation
 *   assigned `order` inside the `context.addInitScript()` closure, which
 *   Playwright re-evaluates on every navigation/reload/frame-attach, so the
 *   sequence silently restarted at 0 after every reload and never proved
 *   cross-reload ordering. `createAuditedContext()` now assigns `order`
 *   from a Node-side counter that lives in this test process (never reset
 *   by navigation), plus a per-navigation `documentEpoch` (bumped on every
 *   main-frame `framenavigated`) and a `epochOrder` sequence local to that
 *   epoch. The final assertions below prove `order` is unique and strictly
 *   increasing across the whole scenario, including across the reload
 *   boundary (`documentEpoch` spans at least two values).
 * - P1 Blocker 2 (fire-and-forget log delivery): the prior
 *   `exposeBinding()` callback was invoked without any drain barrier before
 *   the zero-count assertions ran. The audit mechanism no longer uses
 *   `exposeBinding()` at all (see next point) — it is driven by a raw CDP
 *   session instead, and `flushAudit()` performs a round-trip `send()` on
 *   that SAME CDP session immediately before `page.reload()` and again
 *   immediately before the final assertions. CDP delivers messages on a
 *   single session strictly in the order the browser sent them, so any
 *   `DOMStorage.*` event already dispatched before the round-trip command
 *   was sent is guaranteed to have reached our event handlers before the
 *   round-trip's response resolves.
 * - P1 Blocker 3 (named-property access bypasses the prototype patch):
 *   `Storage.prototype.setItem/removeItem/clear` monkey-patching (the prior
 *   mechanism) cannot intercept `localStorage[key] = value` /
 *   `delete localStorage[key]` — the HTML Standard's Storage exotic-object
 *   [[Set]]/[[Delete]] internal methods do not go through those prototype
 *   methods. `createAuditedContext()` now uses the CDP `DOMStorage` domain
 *   (`DOMStorage.domStorageItemAdded/Updated/Removed/ItemsCleared`), which
 *   observes every localStorage mutation at the browser-engine level,
 *   independent of which JS API triggered it. This is Chromium-specific
 *   (the CDP `DOMStorage` domain); `playwright.config.ts` only configures a
 *   `chromium` project in this repo, so that is not a gap in practice. A
 *   dedicated regression test below
 *   ("write-set audit: adversarial named-property Storage access is
 *   captured") proves the audit still catches `localStorage[key] = value`
 *   / `delete localStorage[key]`, which a pure prototype patch could not.
 *
 * Empirically verified (Issue #1283 repair iteration 1, scratch probe
 * against a throwaway http server + Chromium): `context.newCDPSession(page)`
 * survives `page.reload()` (the CDP session is bound to the page/target,
 * which is not destroyed by a same-tab navigation), and `storageState`
 * seeding applied by Playwright at context-creation time does NOT emit any
 * `DOMStorage.*` events once our CDP session enables the `DOMStorage`
 * domain after the page is created but before the first `page.goto()` — so
 * the audit never spuriously records the initial sentinel/seed writes as
 * app-triggered operations (preserving the existing AC7 "never
 * (re-)written by the app via setItem" semantics).
 */

import { test, expect, type Page, type BrowserContext, type Browser, type CDPSession } from '@playwright/test'
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
// Issue #1283 repair iteration 1 (P1 Blocker 3 regression guard): a key
// distinct from both the production key and the per-test E2E key, used ONLY
// by the dedicated adversarial named-property test below. Never touched by
// the main scenario.
const ADVERSARIAL_NAMED_PROPERTY_KEY = 'loop-protocol.e2e.adversarial-named-property-probe'

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
//
// Issue #1283 repair iteration 1: rebuilt on the CDP `DOMStorage` domain
// (see file header for the three P1 Blockers this replaces). Both spec
// files (`m4-upgrade-loop.spec.ts` and `m4-preview-namespace.spec.ts`)
// duplicate this helper rather than importing a shared module — extracting
// it into a new file would fall outside this Issue's Allowed Paths
// (`tests/e2e/m4-upgrade-loop.spec.ts`, `tests/e2e/m4-preview-namespace.spec.ts`,
// `playwright.config.ts`, `package.json`, `.github/workflows/ci.yml` only),
// so the two copies are kept structurally identical instead.
// ---------------------------------------------------------------------------

type WriteOp = {
  type: 'setItem' | 'removeItem' | 'clear'
  key: string | null
  /** Global, monotonically-increasing sequence number assigned from this
   *  Node-side counter. Never reset by navigation/reload (P1 Blocker 1). */
  order: number
  /** Bumped on every main-frame `framenavigated` (including reload) — proves
   *  operations are attributable to a specific navigation/document, and lets
   *  the final assertions prove the audit actually observed operations
   *  spanning the reload boundary, not just before it (P1 Blocker 1). */
  documentEpoch: number
  /** Sequence local to `documentEpoch`, reset to 0 on every navigation. The
   *  CDP `DOMStorage` domain does not expose a frame/document id, so this
   *  (navigation-epoch, local-sequence) pair is the practical equivalent of
   *  a per-document sequence for a single-frame same-origin scenario. */
  epochOrder: number
}

/**
 * Creates a browser context whose production sentinel and E2E seed are
 * initialized exactly once, at context creation, via
 * `browser.newContext({ storageState })` (never via `addInitScript`, which
 * reruns on every navigation/reload — the exact false-green mechanism the
 * 2026-08-03 repair fixed). Also installs the Node-side write-set
 * operation-history audit via the CDP `DOMStorage` domain (Issue #1283
 * repair iteration 1, P1 Blockers 1-3 — see file header): every
 * setItem/removeItem/clear-equivalent localStorage mutation is observed at
 * the browser-engine level, independent of which JS API triggered it, with
 * a Node-side monotonic `order` that survives reload.
 */
async function createAuditedContext(
  browser: Browser,
  baseURL: string,
  seed: { productionKey: string; productionSentinel: string; e2eKey: string; e2eSeed: string },
): Promise<{ context: BrowserContext; page: Page; writeLog: WriteOp[]; flushAudit: () => Promise<void> }> {
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

  // Runtime-only config (JS globals, not storage). Safe to reinstall on
  // every navigation/reload: this patch never writes to storage — it only
  // configures the app's runtime E2E hooks.
  await context.addInitScript(
    (init: { e2eKey: string }) => {
      ;(window as Window & { __LOOP_STORAGE_KEY__?: string }).__LOOP_STORAGE_KEY__ = init.e2eKey
      ;(
        window as Window & { __LOOP_E2E_BOOTSTRAP__?: { autoStart?: boolean } }
      ).__LOOP_E2E_BOOTSTRAP__ = { autoStart: false }
    },
    { e2eKey: seed.e2eKey },
  )

  const page = await context.newPage()

  // CDP-based write-set audit (Issue #1283 repair iteration 1, P1 Blockers
  // 1-3): attached after the page exists but before the first navigation,
  // so it never observes the storageState seeding above (empirically
  // verified — see file header) and so it is in place before any app code
  // can run.
  let globalOrder = 0
  let documentEpoch = 0
  let epochOrder = 0

  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) {
      documentEpoch += 1
      epochOrder = 0
    }
  })

  const client: CDPSession = await context.newCDPSession(page)
  await client.send('DOMStorage.enable')

  function record(type: WriteOp['type'], key: string | null): void {
    writeLog.push({ type, key, order: globalOrder, documentEpoch, epochOrder })
    globalOrder += 1
    epochOrder += 1
  }

  client.on('DOMStorage.domStorageItemAdded', (event) => {
    if (event.storageId.isLocalStorage) record('setItem', event.key)
  })
  client.on('DOMStorage.domStorageItemUpdated', (event) => {
    if (event.storageId.isLocalStorage) record('setItem', event.key)
  })
  client.on('DOMStorage.domStorageItemRemoved', (event) => {
    if (event.storageId.isLocalStorage) record('removeItem', event.key)
  })
  client.on('DOMStorage.domStorageItemsCleared', (event) => {
    if (event.storageId.isLocalStorage) record('clear', null)
  })

  // P1 Blocker 2 fix: forces delivery of any DOMStorage event the browser
  // already sent on this SAME CDP session before this call resolves (CDP
  // messages on one session are delivered strictly in send order), so
  // callers can await this immediately before reload / before reading
  // writeLog for assertions.
  async function flushAudit(): Promise<void> {
    await client.send('DOMStorage.enable')
  }

  return { context, page, writeLog, flushAudit }
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

    const { context, page, writeLog, flushAudit } = await createAuditedContext(browser, baseURL, {
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

      // Issue #1283 repair iteration 1 (P1 Blocker 1/2): flush the audit
      // before reload so every pre-reload operation is guaranteed to be in
      // writeLog with its documentEpoch/order recorded before the boundary.
      await flushAudit()

      // AC4 / AC7: reload — the audit CDP session survives reload (attached
      // to the page/target, not the document), so operations after this
      // point are recorded in the next documentEpoch. The production
      // sentinel / E2E seed are NOT re-applied (they were context-level
      // storageState, applied exactly once).
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

      // Issue #1283 repair iteration 1 (P1 Blocker 1 harness proof): without
      // a genuine in-scope write AFTER the reload, the write-set audit's
      // "order is unique and strictly increasing across the reload
      // boundary" assertion below would be vacuous (all recorded operations
      // would be pre-reload). Clicking the preparation screen's real
      // `data-action="save"` button here would NOT produce this: per the
      // HTML Standard, Storage.setItem(key, value) performs no mutation
      // (and fires no storage/DOMStorage event) when `value` is byte-for-byte
      // identical to the value already stored -- empirically confirmed
      // against this exact Chromium build (Issue #1283 repair iteration 1
      // scratch probe) -- and at this point the saved snapshot (resources 0,
      // weaponPower 2) is unchanged from what reload already persisted, so a
      // same-value re-save is a genuine no-op with no observable
      // engine-level mutation to audit. This harness-only write (still
      // through the real `localStorage.setItem` browser API the CDP audit
      // observes -- not a synthetic audit-only hook) writes a DIFFERENT
      // value to the real E2E-scoped key specifically to prove the audit
      // captures a genuine post-reload in-scope mutation; it is a harness
      // self-verification step and is not asserted against any
      // product-visible HUD/state.
      const savedBeforePostReloadMarker = await readStorageKey(page, e2eKey)
      await page.evaluate(
        ({ key, snapshot }: { key: string; snapshot: StoredSnapshot }) => {
          window.localStorage.setItem(key, JSON.stringify(snapshot))
        },
        {
          key: e2eKey,
          snapshot: {
            ...savedBeforePostReloadMarker!,
            resources: savedBeforePostReloadMarker!.resources + 1,
          },
        },
      )

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

      // Issue #1283 repair iteration 1 (P1 Blocker 2): drain barrier before
      // the zero-count / ordering assertions below — guarantees every
      // DOMStorage event the browser already dispatched has been delivered
      // to writeLog before we read it.
      await flushAudit()

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

      // Issue #1283 repair iteration 1 (P1 Blocker 1): order must be unique
      // and strictly increasing across the ENTIRE scenario, not merely
      // "some entries exist" — proves the Node-side global counter was
      // never reset by navigation/reload.
      const orders = writeLog.map((op) => op.order)
      expect(
        orders,
        'write-set audit order values must be unique across the full scenario (P1 Blocker 1)',
      ).toEqual([...new Set(orders)])
      for (let i = 1; i < orders.length; i += 1) {
        expect(
          orders[i],
          `write-set audit order must be strictly increasing at index ${i} (P1 Blocker 1)`,
        ).toBeGreaterThan(orders[i - 1])
      }

      // Issue #1283 repair iteration 1 (P1 Blocker 1): the audit must have
      // observed in-scope operations on BOTH sides of the reload boundary —
      // otherwise the ordering assertion above would be vacuous proof of
      // "ordering across reload" (all operations could have happened
      // pre-reload only). The explicit re-save above (after Load Game
      // restore) guarantees at least one post-reload in-scope write exists.
      const epochsWithInScopeOps = new Set(inScopeOps.map((op) => op.documentEpoch))
      expect(
        epochsWithInScopeOps.size,
        'write-set audit must have observed in-scope operations spanning at least two ' +
          'navigation epochs (before AND after the reload boundary) (P1 Blocker 1)',
      ).toBeGreaterThanOrEqual(2)

      // Issue #1283 repair iteration 1 (P1 Blocker 1): every operation in a
      // later documentEpoch must have a strictly greater global `order` than
      // every operation in an earlier documentEpoch — proves the global
      // counter (not just per-epoch epochOrder) actually orders across the
      // reload boundary, not merely within each epoch independently.
      const maxOrderByEpoch = new Map<number, number>()
      const minOrderByEpoch = new Map<number, number>()
      for (const op of writeLog) {
        maxOrderByEpoch.set(op.documentEpoch, Math.max(maxOrderByEpoch.get(op.documentEpoch) ?? -Infinity, op.order))
        minOrderByEpoch.set(op.documentEpoch, Math.min(minOrderByEpoch.get(op.documentEpoch) ?? Infinity, op.order))
      }
      const epochsSorted = [...maxOrderByEpoch.keys()].sort((a, b) => a - b)
      for (let i = 1; i < epochsSorted.length; i += 1) {
        const previousEpochMax = maxOrderByEpoch.get(epochsSorted[i - 1])!
        const currentEpochMin = minOrderByEpoch.get(epochsSorted[i])!
        expect(
          currentEpochMin,
          `write-set audit order for documentEpoch ${epochsSorted[i]} must exceed every order ` +
            `recorded in documentEpoch ${epochsSorted[i - 1]} (P1 Blocker 1: proves global ` +
            'ordering, not per-epoch-only ordering)',
        ).toBeGreaterThan(previousEpochMax)
      }
    } finally {
      await context.close()
    }
  },
)

test(
  'M4 upgrade loop write-set audit: adversarial named-property Storage access is captured (Issue #1283 P1 Blocker 3 regression guard)',
  async ({ browser }, testInfo) => {
    test.setTimeout(30_000)

    const e2eKey = buildE2EStorageKey(testInfo)
    const e2eSeed = JSON.stringify({
      schemaVersion: 1,
      resources: 100,
      weaponPower: 1,
      playerMaxHp: 8,
    })
    const baseURL = (testInfo.project.use.baseURL as string | undefined) ?? 'http://127.0.0.1:4173'

    const { context, page, writeLog, flushAudit } = await createAuditedContext(browser, baseURL, {
      productionKey: PRODUCTION_KEY,
      productionSentinel: PRODUCTION_SENTINEL,
      e2eKey,
      e2eSeed,
    })

    try {
      await page.goto('./')

      // A pure Storage.prototype.setItem/removeItem/clear monkey-patch (the
      // audit mechanism this Issue's prior iteration used) CANNOT intercept
      // named-property assignment/deletion — the HTML Standard's Storage
      // [[Set]]/[[Delete]] internal methods bypass those prototype methods
      // entirely. This proves the CDP-based audit catches it anyway.
      await page.evaluate((key: string) => {
        ;(window.localStorage as unknown as Record<string, string>)[key] = 'adversarial-value'
        delete (window.localStorage as unknown as Record<string, string>)[key]
      }, ADVERSARIAL_NAMED_PROPERTY_KEY)

      await flushAudit()

      const adversarialOps = writeLog.filter((op) => op.key === ADVERSARIAL_NAMED_PROPERTY_KEY)
      expect(
        adversarialOps.map((op) => op.type),
        'named-property assignment (`localStorage[key] = value`) followed by named-property ' +
          'deletion (`delete localStorage[key]`) must both be captured by the write-set audit, ' +
          'even though neither goes through Storage.prototype.setItem/removeItem (P1 Blocker 3)',
      ).toEqual(['setItem', 'removeItem'])
    } finally {
      await context.close()
    }
  },
)
